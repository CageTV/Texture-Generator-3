"""
Texture Generator v1.2
Self-contained portable Windows GUI.
No ImageMagick required — format conversions via PIL + texconv.
texconv.exe is bundled inside this executable.
"""

import os, sys, subprocess, shutil, threading, tempfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from PIL import Image, ImageTk
import numpy as np
import cv2

import platform_tools as _pt

try:
    import pbr_engine as _pe
    _PBR_OK = True
except ImportError:
    _PBR_OK = False

try:
    import material_engine as _me
    _MAT_OK = True
except ImportError:
    _MAT_OK = False

try:
    import material_preview as _mp
    _MP_OK = True
except ImportError:
    _MP_OK = False

try:
    import gpu_engine as _ge
    _GPU_OK = _ge.gpu_available()   # probes for a real GL context, not just the import
except Exception:
    _GPU_OK = False

_preview_win = None  # singleton


# ─── Paths ────────────────────────────────────────────────────────────────────
def resource_path(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

def texconv_exe():
    """Windows only -- kept for any code path that still wants the raw
    exe path directly. Prefer _pt.texconv_available()/dds_decode/dds_encode."""
    return resource_path('texconv.exe')

def bsarch_exe():
    return resource_path('BSArch.exe')

# BSA archive format options for packing
_BSA_PACK_FORMATS = [
    ('sse',    'Skyrim Special Edition'),
    ('tes5',   'Skyrim LE  (same as fo3 / fnv)'),
    ('fo4',    'Fallout 4 General'),
    ('fo4dds', 'Fallout 4 DDS  (streamed textures)'),
    ('tes4',   'Oblivion'),
    ('fo3',    'Fallout 3'),
    ('fnv',    'Fallout: New Vegas'),
    ('tes3',   'Morrowind'),
]


# ─── DDS smart-format table (from DDSTool) ───────────────────────────────────
_DDS_RULES = [
    (('_n',),                       'BC7_UNORM',      'Normal map'),
    (('_p', '_height'),             'BC4_UNORM',      'Height / Parallax'),
    (('_rmaos', '_ao', '_orm'),     'BC7_UNORM',      'RMAOS / AO / ORM'),
    (('_s', '_specular'),           'BC7_UNORM',      'Specular'),
    (('_g', '_glow', '_emissive'),  'BC7_UNORM_SRGB', 'Emissive'),
    (('_m', '_mask'),               'BC4_UNORM',      'Mask'),
]
_DDS_DEFAULT = ('BC7_UNORM_SRGB', 'Albedo / Diffuse')

_DDS_FMT_OPTIONS = [
    'BC7_UNORM', 'BC7_UNORM_SRGB',
    'BC5_UNORM', 'BC5_SNORM',
    'BC4_UNORM', 'BC4_SNORM',
    'BC3_UNORM', 'BC3_UNORM_SRGB',
    'BC1_UNORM', 'BC1_UNORM_SRGB',
    'R8G8B8A8_UNORM', 'R8G8B8A8_UNORM_SRGB',
]

def smart_dds_format(filename):
    """Return (dds_format, description) based on filename suffix."""
    stem = Path(filename).stem.lower()
    for suffixes, fmt, desc in _DDS_RULES:
        if any(stem.endswith(s) for s in suffixes):
            return fmt, desc
    return _DDS_DEFAULT


# ─── texconv helpers ─────────────────────────────────────────────────────────
def _tc_extract(src, out_dir):
    """DDS/any → PNG in out_dir. Returns PNG path or None."""
    return _pt.dds_decode(src, out_dir)

def _tc_compress(src, dst, fmt):
    """src → DDS at dst with given format."""
    return _pt.dds_encode(src, dst, fmt)


# ─── Normal map generator ────────────────────────────────────────────────────
def generate_normal_map(img, scale=10.0, use_x=True, use_y=True,
                        flip_x=False, flip_y=False, full_z=False):
    arr = np.array(img.convert('L')).astype('float32') / 255.0
    dx = np.gradient(arr, axis=1) * scale if use_x else np.zeros_like(arr)
    dy = np.gradient(arr, axis=0) * scale if use_y else np.zeros_like(arr)
    if flip_x: dx = -dx
    if flip_y: dy = -dy
    dz = np.ones_like(arr)
    length = np.sqrt(dx**2 + dy**2 + dz**2)
    nx = ((dx / length) + 1.0) * 127.5
    ny = ((dy / length) + 1.0) * 127.5
    nz = (dz / length) * 255.0 if full_z else ((dz / length) + 1.0) * 127.5
    return Image.fromarray(np.stack([nx, ny, nz], axis=-1).clip(0, 255).astype('uint8'))


def generate_normal_map_auto(img, scale, use_x, use_y, flip_x, flip_y, full_z, use_gpu):
    """Picks the GPU compute-shader path when requested/available, otherwise
    (or on any GPU failure) falls back to the CPU NumPy path. Returns (image, backend_str)."""
    if use_gpu and _GPU_OK:
        try:
            img_out = _ge.gpu_normal_from_image(img, scale, use_x, use_y, flip_x, flip_y, full_z)
            return img_out, 'gpu'
        except Exception:
            pass  # fall through to CPU below
    return generate_normal_map(img, scale, use_x, use_y, flip_x, flip_y, full_z), 'cpu'


# ─── Color palette ────────────────────────────────────────────────────────────
C = {
    'bg':          '#1e1e1e',
    'surface':     '#252526',
    'panel':       '#2d2d2d',
    'panel2':      '#323232',
    'input':       '#3c3c3c',
    'border':      '#474747',
    'text':        '#cccccc',
    'text_dim':    '#858585',
    'text_bright': '#e8e8e8',
    'accent':      '#0078d4',
    'accent_hi':   '#1a86d9',
    'success':     '#4ec9b0',
    'warn':        '#ce9178',
    'error':       '#f44747',
    'exp':         '#ff8c00',
    'con_bg':      '#0d0d0d',
    'con_fg':      '#4ec9b0',
}


# ═════════════════════════════════════════════════════════════════════════════
class TextureGeneratorApp:

    W, H = 1100, 780

    def __init__(self, root):
        self.root = root
        self.root.title('Texture Generator')
        self.root.geometry(f'{self.W}x{self.H}')
        self.root.minsize(960, 680)
        self.root.configure(bg=C['bg'])

        self.input_dir   = tk.StringVar(value=os.getcwd())
        self.output_dir  = tk.StringVar(value=os.getcwd())
        self.texconv_ok  = _pt.texconv_available()
        self.last_nm_path = None

        self._setup_styles()
        self._build_ui()

    # ── Styles ────────────────────────────────────────────────────────────────
    def _setup_styles(self):
        s = ttk.Style(); s.theme_use('clam')
        s.configure('TNotebook', background=C['surface'], borderwidth=0, tabmargins=0)
        s.configure('TNotebook.Tab', background=C['panel'], foreground=C['text_dim'],
                    padding=[15, 8], font=('Segoe UI', 9, 'bold'), borderwidth=0)
        s.map('TNotebook.Tab',
              background=[('selected', C['surface']), ('active', C['input'])],
              foreground=[('selected', C['text_bright']), ('active', C['text'])])
        s.configure('TSeparator', background=C['border'])
        s.configure('TScrollbar', background=C['panel'], troughcolor=C['surface'],
                    arrowcolor=C['text_dim'])
        for sty, bg in [('Dark.TCheckbutton', C['surface']),
                        ('Panel.TCheckbutton', C['panel'])]:
            s.configure(sty, background=bg, foreground=C['text'],
                        font=('Segoe UI', 9), focuscolor=bg)
            s.map(sty, background=[('active', bg)], foreground=[('active', C['text_bright'])])
        s.configure('Dark.TRadiobutton', background=C['surface'], foreground=C['text'],
                    font=('Segoe UI', 9), focuscolor=C['surface'])
        s.map('Dark.TRadiobutton', background=[('active', C['surface'])],
              foreground=[('active', C['text_bright'])])

    # ── UI shell ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.bg_canvas = tk.Canvas(self.root, highlightthickness=0, bg=C['bg'])
        self.bg_canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._load_bg()
        self.root.bind('<Configure>', lambda e: e.widget is self.root and self._render_bg())

        # Header
        hdr = tk.Frame(self.root, bg='#131313', height=52)
        hdr.pack(fill='x'); hdr.pack_propagate(False)
        tk.Label(hdr, text='⬡  TEXTURE GENERATOR', bg='#131313', fg=C['text_bright'],
                 font=('Segoe UI', 13, 'bold')).pack(side='left', padx=18, pady=10)
        self.bsarch_ok  = _pt.bsarch_available()
        tc_col  = C['success'] if self.texconv_ok  else C['error']
        bsa_col = C['success'] if self.bsarch_ok   else C['error']
        tk.Label(hdr, text=f' {"✓" if self.texconv_ok else "✗"} texconv  ',
                 bg='#1a1a1a', fg=tc_col,
                 font=('Segoe UI', 8, 'bold'), padx=6).pack(side='right', padx=2, pady=14)
        tk.Label(hdr, text=f' {"✓" if self.bsarch_ok else "✗"} BSArch  ',
                 bg='#1a1a1a', fg=bsa_col,
                 font=('Segoe UI', 8, 'bold'), padx=6).pack(side='right', padx=2, pady=14)
        tk.Label(hdr, text=' ✓ PIL  ', bg='#1a1a1a', fg=C['success'],
                 font=('Segoe UI', 8, 'bold'), padx=6).pack(side='right', padx=2, pady=14)

        # Input / Output bar
        dbar = tk.Frame(self.root, bg=C['surface'], height=54)
        dbar.pack(fill='x'); dbar.pack_propagate(False)
        for i, (lbl, var, fn) in enumerate([
            ('Input Folder:',  self.input_dir,  self._browse_input),
            ('Output Folder:', self.output_dir, self._browse_output),
        ]):
            row = tk.Frame(dbar, bg=C['surface'])
            row.pack(fill='x', padx=8, pady=(4 if i == 0 else 1, 0))
            tk.Label(row, text=lbl, bg=C['surface'], fg=C['text_dim'],
                     font=('Segoe UI', 8), width=13, anchor='w').pack(side='left', padx=(6,2))
            tk.Entry(row, textvariable=var, bg=C['input'], fg=C['text'],
                     insertbackground=C['text'], relief='flat', font=('Segoe UI', 8), bd=2
                     ).pack(side='left', fill='x', expand=True, padx=2)
            tk.Button(row, text=' … ', command=fn,
                      bg=C['accent'], fg='white',
                      activebackground=C['accent_hi'], activeforeground='white',
                      relief='flat', cursor='hand2', font=('Segoe UI', 9, 'bold'), padx=8, pady=1
                      ).pack(side='right', padx=(4,6))

        # Notebook — 4 tabs
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill='both', expand=True)

        t1 = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t1, text='  Texture Converters  ')
        self._build_converters_tab(t1)

        t2 = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t2, text='  DDS Tool  ')
        self._build_dds_tool_tab(t2)

        t_bsa = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t_bsa, text='  BSA Utilities  ')
        self._build_bsa_tab(t_bsa)

        t3 = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t3, text='  Normal Map Generator  ')
        self._build_normal_maps_tab(t3)

        t_pmap = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t_pmap, text='  Height Map Generator  ')
        self._build_pmap_tab(t_pmap)

        t4 = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t4, text='  PBR Batch Generator  ')
        self._build_pbr_tab(t4)

        t_dual = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t_dual, text='  Dual Layer Builder  ')
        self._build_dual_layer_tab(t_dual)

        t_mat = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t_mat, text='  Material Generator  ')
        self._build_material_tab(t_mat)

        t_json = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t_json, text='  PBR JSON Builders  ')
        self._build_pbr_json_split_tab(t_json)

    # ── Background ────────────────────────────────────────────────────────────
    def _load_bg(self):
        try:
            self._bg_pil = Image.open(resource_path('background.png')).convert('RGBA')
            self._render_bg()
        except Exception:
            self._bg_pil = None

    def _render_bg(self):
        if not getattr(self, '_bg_pil', None): return
        w = max(self.root.winfo_width(), self.W)
        h = max(self.root.winfo_height(), self.H)
        img = self._bg_pil.copy().resize((w, h), Image.LANCZOS)
        ov = Image.new('RGBA', (w, h), (0, 0, 0, 200))
        img = Image.alpha_composite(img, ov).convert('RGB')
        self._bg_photo = ImageTk.PhotoImage(img)
        self.bg_canvas.delete('bg')
        self.bg_canvas.create_image(0, 0, anchor='nw', image=self._bg_photo, tags='bg')
        self.bg_canvas.lower('bg')

    # ── Generic helpers ───────────────────────────────────────────────────────
    def _mkbtn(self, parent, text, cmd, bg=None, fg='white', pad=(12,6), font=None, **kw):
        return tk.Button(parent, text=text, command=cmd,
                         bg=bg or C['accent'], fg=fg,
                         activebackground=C['accent_hi'], activeforeground='white',
                         relief='flat', cursor='hand2', bd=0,
                         font=font or ('Segoe UI', 9, 'bold'),
                         padx=pad[0], pady=pad[1], **kw)

    def _section_lbl(self, p, title, sub='', bg=None):
        bg = bg or C['surface']
        tk.Label(p, text=title, bg=bg, fg=C['accent'],
                 font=('Segoe UI', 10, 'bold')).pack(anchor='w', padx=16, pady=(14,1))
        if sub:
            tk.Label(p, text=sub, bg=bg, fg=C['text_dim'],
                     font=('Segoe UI', 8)).pack(anchor='w', padx=16, pady=(0,6))

    def _sep(self, p):
        ttk.Separator(p).pack(fill='x', padx=16, pady=10)

    def _console(self, p, **kw):
        return scrolledtext.ScrolledText(p, bg=C['con_bg'], fg=C['con_fg'],
            insertbackground=C['text'], font=('Consolas', 8),
            relief='flat', wrap='word', state='disabled', **kw)

    def _log(self, w, msg, fg=None):
        w.configure(state='normal')
        start = w.index('end-1c')
        w.insert('end', msg + '\n')
        if fg:
            end = w.index('end-1c')
            tag = f't{fg.replace("#","")}'
            w.tag_configure(tag, foreground=fg)
            w.tag_add(tag, start, end)
        w.see('end'); w.configure(state='disabled')

    def _clear_log(self, w):
        w.configure(state='normal'); w.delete('1.0','end'); w.configure(state='disabled')

    def _run_thread(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    @staticmethod
    def _make_scrollable(parent, bg):
        canvas = tk.Canvas(parent, bg=bg, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
        frame = tk.Frame(canvas, bg=bg)
        frame.bind('<Configure>',
                   lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        win_id = canvas.create_window((0, 0), window=frame, anchor='nw')
        canvas.configure(yscrollcommand=sb.set)
        # KEY: stretch inner frame to canvas width so widgets always fill the panel
        canvas.bind('<Configure>',
                    lambda e, wid=win_id: canvas.itemconfig(wid, width=e.width))
        canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        canvas.bind_all('<MouseWheel>',
                        lambda e: canvas.yview_scroll(-1*(e.delta//120), 'units'))
        return canvas, frame

    def _browse_input(self):
        d = filedialog.askdirectory(initialdir=self.input_dir.get())
        if d: self.input_dir.set(os.path.normpath(d))

    def _browse_output(self):
        d = filedialog.askdirectory(initialdir=self.output_dir.get())
        if d: self.output_dir.set(os.path.normpath(d))

    def _find(self, *exts):
        result = []
        for root, _, files in os.walk(self.input_dir.get()):
            for f in files:
                if any(f.lower().endswith(f'.{e.lower()}') for e in exts):
                    result.append(os.path.join(root, f))
        return result

    def _mirror_out(self, src, new_ext):
        rel = os.path.relpath(src, self.input_dir.get())
        out = os.path.join(self.output_dir.get(),
                           os.path.splitext(rel)[0] + f'.{new_ext}')
        os.makedirs(os.path.dirname(out), exist_ok=True)
        return out

    def _need_texconv(self, log_w=None):
        if not self.texconv_ok:
            msg = 'texconv.exe not found. Rebuild the exe to bundle it.'
            if log_w:
                self._log(log_w, msg, C['error'])
            else:
                messagebox.showerror('texconv Missing', msg)
            return False
        return True

    # ── PIL conversion helpers ────────────────────────────────────────────────
    def _pil_convert(self, src, dst, mode='RGBA'):
        """Convert any non-DDS format via PIL."""
        img = Image.open(src)
        if mode:
            img = img.convert(mode)
        img.save(dst)

    def _dds_to_img(self, src, dst):
        """DDS → any format: texconv extracts to PNG, PIL converts to target."""
        with tempfile.TemporaryDirectory() as tmp:
            png = _tc_extract(src, tmp)
            if not png:
                return False
            ext = Path(dst).suffix.lower()
            img = Image.open(png)
            img = img.convert('RGB' if ext == '.bmp' else 'RGBA')
            img.save(dst)
        return True

    def _img_to_dds(self, src, dst, fmt):
        """any format → DDS: PIL converts to PNG if needed, texconv compresses."""
        src = Path(src)
        if src.suffix.lower() == '.dds':
            return _tc_compress(src, dst, fmt)
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / (src.stem + '.png')
            img = Image.open(src).convert('RGBA')
            img.save(str(png))
            return _tc_compress(str(png), dst, fmt)

    def _do_conversion(self, src_ext, dst_ext, log_w, done_msg, conv_fn):
        """Generic batch conversion runner."""
        files = self._find(src_ext)
        if not files:
            self._log(log_w, f'No .{src_ext.upper()} in: {self.input_dir.get()}', C['warn'])
            return
        self._log(log_w,
                  f'Input:  {self.input_dir.get()}\n'
                  f'Output: {self.output_dir.get()}\nFound {len(files)} file(s)…')
        ok = fail = 0
        for f in files:
            out = self._mirror_out(f, dst_ext)
            rel = os.path.relpath(f, self.input_dir.get())
            self._log(log_w, f'  {rel}')
            try:
                result = conv_fn(f, out)
                if result is False:
                    raise RuntimeError('conversion returned False')
                self._log(log_w, f'    ✓', C['success']); ok += 1
            except Exception as e:
                self._log(log_w, f'    ✗ {e}', C['error']); fail += 1
        self._log(log_w, f'\n{done_msg}  ({ok} ok, {fail} failed)\n', C['success'])

    # =========================================================================
    # TAB 1 – TEXTURE CONVERTERS  (PIL + texconv, no ImageMagick)
    # =========================================================================
    def _build_converters_tab(self, parent):
        left = tk.Frame(parent, bg=C['surface'])
        left.pack(side='left', fill='both', expand=True)
        right = tk.Frame(parent, bg=C['panel'], width=310)
        right.pack(side='right', fill='y'); right.pack_propagate(False)

        tk.Label(right, text='OUTPUT LOG', bg=C['panel'], fg=C['text_dim'],
                 font=('Consolas', 8, 'bold')).pack(anchor='w', padx=10, pady=(10,2))
        self.conv_log = self._console(right)
        self.conv_log.pack(fill='both', expand=True, padx=8, pady=(0,4))
        self._mkbtn(right, 'Clear', lambda: self._clear_log(self.conv_log),
                    bg=C['panel2'], fg=C['text_dim'], pad=(8,3),
                    font=('Segoe UI', 8)).pack(anchor='e', padx=8, pady=(0,8))

        _, sf = self._make_scrollable(left, C['surface'])
        self._section_lbl(sf, 'FORMAT CONVERSIONS',
            'No ImageMagick required — uses PIL + texconv  |  Input → Output folder  |  Recursive')

        grid = tk.Frame(sf, bg=C['surface'])
        grid.pack(fill='x', padx=16, pady=4)
        grid.columnconfigure(0, weight=1); grid.columnconfigure(1, weight=1)

        defs = [
            ('BMP  →  TGA',            'BMP','TGA', lambda f,o: self._pil_convert(f, o, 'RGBA')),
            ('TGA  →  BMP (no alpha)', 'TGA','BMP', lambda f,o: self._pil_convert(f, o, 'RGB')),
            ('PNG  →  TGA',            'PNG','TGA', lambda f,o: self._pil_convert(f, o, 'RGBA')),
            ('TGA  →  PNG',            'TGA','PNG', lambda f,o: self._pil_convert(f, o, 'RGBA')),
            ('BMP  →  PNG',            'BMP','PNG', lambda f,o: self._pil_convert(f, o, 'RGBA')),
            ('PNG  →  BMP',            'PNG','BMP', lambda f,o: self._pil_convert(f, o, 'RGB')),
            ('DDS  →  PNG  ⚡',        'DDS','PNG', lambda f,o: _tc_extract(f, Path(o).parent) and True),
            ('DDS  →  TGA  ⚡',        'DDS','TGA', lambda f,o: self._dds_to_img(f, o)),
            ('DDS  →  BMP  ⚡',        'DDS','BMP', lambda f,o: self._dds_to_img(f, o)),
            ('PNG  →  DDS  ⚡',        'PNG','DDS', lambda f,o: self._img_to_dds(f, o, smart_dds_format(f)[0])),
            ('TGA  →  DDS  ⚡',        'TGA','DDS', lambda f,o: self._img_to_dds(f, o, smart_dds_format(f)[0])),
            ('BMP  →  DDS  ⚡',        'BMP','DDS', lambda f,o: self._img_to_dds(f, o, smart_dds_format(f)[0])),
        ]
        for i, (lbl, src, dst, fn) in enumerate(defs):
            s, d = src.lower(), dst.lower()
            needs_tc = '⚡' in lbl
            card = tk.Frame(grid, bg=C['panel'])
            card.grid(row=i//2, column=i%2, padx=5, pady=5, sticky='nsew')
            tk.Label(card, text=lbl, bg=C['panel'], fg=C['text_bright'],
                     font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=10, pady=(8,1))
            tc_note = '  texconv' if needs_tc else '  PIL only'
            tk.Label(card, text=tc_note, bg=C['panel'], fg=C['text_dim'],
                     font=('Segoe UI', 7)).pack(anchor='w', padx=10)

            def _make_cmd(src_e=s, dst_e=d, conv=fn, label=lbl):
                def cmd():
                    if '⚡' in label and not self._need_texconv(self.conv_log): return
                    self._run_thread(lambda: (
                        self._clear_log(self.conv_log),
                        self._log(self.conv_log, f'▶ {label.replace("  ⚡","")}'),
                        self._do_conversion(src_e, dst_e, self.conv_log,
                                            f'{label.replace("  ⚡","")} complete.', conv)
                    ))
                return cmd
            self._mkbtn(card, '▶  Run', _make_cmd(), pad=(10,4)).pack(anchor='e', padx=8, pady=6)

        self._sep(sf)
        self._section_lbl(sf, 'COLOR CHANNEL OPERATIONS',
            'Zero-out a color channel in ALL BMP files in Input Folder (in-place, pure PIL)')
        ch_row = tk.Frame(sf, bg=C['surface'])
        ch_row.pack(fill='x', padx=16, pady=4)
        for lbl, idx, col in [('Remove Red',   0, '#e74856'),
                               ('Remove Green', 1, '#4ec9b0'),
                               ('Remove Blue',  2, '#569cd6')]:
            tk.Button(ch_row, text=lbl, command=lambda i=idx, n=lbl: self._rm_channel(i, n),
                      bg=C['panel'], fg=col, activebackground=C['input'],
                      activeforeground=col, relief='flat', cursor='hand2',
                      font=('Segoe UI', 9, 'bold'), padx=14, pady=7, bd=0
                      ).pack(side='left', padx=6, pady=4)

        self._sep(sf)
        self._section_lbl(sf, 'FILE MANAGEMENT')
        keep_row = tk.Frame(sf, bg=C['surface'])
        keep_row.pack(fill='x', padx=16, pady=(0,6))
        tk.Button(keep_row, text='Keep Only _n BMP', command=self._keep_n_bmps,
                  bg=C['panel'], fg=C['warn'], activebackground=C['input'],
                  activeforeground=C['warn'], relief='flat', cursor='hand2',
                  font=('Segoe UI', 9, 'bold'), padx=14, pady=7, bd=0
                  ).pack(side='left', padx=6, pady=4)
        del_row = tk.Frame(sf, bg=C['surface'])
        del_row.pack(fill='x', padx=16, pady=(0,4))
        tk.Label(del_row, text='Delete all:',
                 bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8)).pack(side='left', padx=(0,8), pady=6)
        for ext in ['BMP','TGA','DDS','PNG']:
            tk.Button(del_row, text=f' {ext} ',
                      command=lambda e=ext.lower(): self._rm_ext(e),
                      bg='#3a1515', fg=C['error'], activebackground='#5a2424',
                      activeforeground=C['error'], relief='flat', cursor='hand2',
                      font=('Segoe UI', 9,'bold'), padx=6, pady=5, bd=0
                      ).pack(side='left', padx=4)
        tk.Frame(sf, bg=C['surface'], height=16).pack()

    def _rm_channel(self, idx, name):
        if not messagebox.askyesno('Confirm',
            f'Remove {name} channel from ALL BMP in:\n{self.input_dir.get()}\n\nIn-place. Continue?'):
            return
        def r():
            self._clear_log(self.conv_log)
            self._log(self.conv_log, f'▶ Removing {name} channel (PIL)…')
            for f in self._find('bmp'):
                try:
                    img = Image.open(f).convert('RGB')
                    arr = np.array(img); arr[:,:,idx] = 0
                    Image.fromarray(arr).save(f)
                    self._log(self.conv_log, f'  ✓ {os.path.basename(f)}', C['success'])
                except Exception as e:
                    self._log(self.conv_log, f'  ✗ {os.path.basename(f)}: {e}', C['error'])
            self._log(self.conv_log, 'Done.\n', C['success'])
        self._run_thread(r)

    def _rm_ext(self, ext):
        if not messagebox.askyesno('Confirm Delete',
            f'Delete ALL .{ext.upper()} in:\n{self.input_dir.get()}\n\nCannot be undone.'): return
        def r():
            self._clear_log(self.conv_log)
            files = self._find(ext)
            if not files:
                self._log(self.conv_log, f'No .{ext.upper()} found.', C['warn']); return
            for f in files:
                try:
                    os.remove(f)
                    self._log(self.conv_log, f'  ✓ {os.path.basename(f)}', C['success'])
                except Exception as e:
                    self._log(self.conv_log, f'  ✗ {e}', C['error'])
            self._log(self.conv_log, 'Done.\n', C['success'])
        self._run_thread(r)

    def _keep_n_bmps(self):
        if not messagebox.askyesno('Confirm',
            f'Delete all BMP without "_n" in:\n{self.input_dir.get()}'): return
        def r():
            self._clear_log(self.conv_log)
            for f in self._find('bmp'):
                if '_n.' in os.path.basename(f).lower():
                    self._log(self.conv_log, f'  → Kept: {os.path.basename(f)}')
                else:
                    try:
                        os.remove(f)
                        self._log(self.conv_log,
                                  f'  ✓ Removed: {os.path.basename(f)}', C['success'])
                    except Exception as e:
                        self._log(self.conv_log, f'  ✗ {e}', C['error'])
            self._log(self.conv_log, 'Done.\n', C['success'])
        self._run_thread(r)

    # =========================================================================
    # TAB 2 – DDS TOOL  (interactive format assignment + smart compression)
    # =========================================================================
    def _build_dds_tool_tab(self, parent):
        # Split: left controls (scrollable), right log
        left_outer = tk.Frame(parent, bg=C['surface'], width=500)
        left_outer.pack(side='left', fill='y')
        left_outer.pack_propagate(False)
        right = tk.Frame(parent, bg=C['panel'])
        right.pack(side='right', fill='both', expand=True)

        # Scrollable left pane
        _, sf = self._make_scrollable(left_outer, C['surface'])

        self._section_lbl(sf, 'DDS SMART COMPRESSOR',
            'Converts PNG or DDS → DDS using texconv. Assign formats per texture type below.')

        src_var = self.input_dir
        out_var = self.output_dir
        self._folder_row(sf, 'Source Folder', src_var)
        self._folder_row(sf, 'Output Folder', out_var)

        self._sep(sf)
        tk.Label(sf, text='INPUT MODE', bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=16)
        mode_var = tk.StringVar(value='png')
        for val, lbl in [('png', 'PNG → DDS  (compress source art)'),
                         ('dds', 'DDS → DDS  (recompress / change format)')]:
            ttk.Radiobutton(sf, text=lbl, variable=mode_var, value=val,
                            style='Dark.TRadiobutton').pack(anchor='w', padx=22, pady=2)

        self._sep(sf)

        # ── Format assignment rules ───────────────────────────────────────────
        hdr_row = tk.Frame(sf, bg=C['surface'])
        hdr_row.pack(fill='x', padx=16, pady=(0, 4))
        tk.Label(hdr_row, text='FORMAT ASSIGNMENT RULES', bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 9, 'bold')).pack(side='left')
        self._mkbtn(hdr_row, '↺ Reset', self._reset_dds_rules,
                    bg=C['panel'], fg=C['text_dim'], pad=(8, 3),
                    font=('Segoe UI', 8)).pack(side='right')
        self._mkbtn(hdr_row, '+ Add Rule', self._add_dds_rule,
                    bg=C['panel'], fg=C['text'], pad=(8, 3),
                    font=('Segoe UI', 8)).pack(side='right', padx=(0, 6))

        # Column headers
        col_hdr = tk.Frame(sf, bg=C['surface'])
        col_hdr.pack(fill='x', padx=16, pady=(0, 2))
        col_hdr.columnconfigure(0, weight=3)
        col_hdr.columnconfigure(1, weight=4)
        col_hdr.columnconfigure(2, minsize=24)
        for c, h in enumerate(['Suffix(es)  (comma-separated)', 'DDS Format', '']):
            tk.Label(col_hdr, text=h, bg=C['surface'], fg=C['text_dim'],
                     font=('Segoe UI', 7, 'bold'), anchor='w'
                     ).grid(row=0, column=c, sticky='ew', padx=(0, 4))

        # Container for dynamic rule rows
        self._dds_rules_container = tk.Frame(sf, bg=C['surface'])
        self._dds_rules_container.pack(fill='x', padx=16, pady=(0, 4))

        # Init rule vars from defaults and draw them
        self._dds_rule_vars = []   # list of (suffixes_var, fmt_var)
        self._init_dds_rules()
        self._rebuild_dds_rules_ui()

        # Default fallback row
        self._sep(sf)
        def_row = tk.Frame(sf, bg=C['surface'])
        def_row.pack(fill='x', padx=16, pady=(0, 4))
        tk.Label(def_row, text='Default (unmatched files):', bg=C['surface'],
                 fg=C['text_dim'], font=('Segoe UI', 8)).pack(side='left', padx=(0, 8))
        self._dds_default_var = tk.StringVar(value=_DDS_DEFAULT[0])
        ttk.Combobox(def_row, textvariable=self._dds_default_var,
                     values=_DDS_FMT_OPTIONS, state='readonly',
                     font=('Consolas', 8), width=20
                     ).pack(side='left')
        tk.Label(def_row, text='← Albedo / Diffuse', bg=C['surface'],
                 fg=C['text_dim'], font=('Segoe UI', 8)).pack(side='left', padx=(8, 0))

        self._sep(sf)
        self._mkbtn(sf, '▶  Run DDS Compression',
                    lambda: self._run_dds_tool(src_var, out_var, mode_var),
                    pad=(16, 10), font=('Segoe UI', 10, 'bold')
                    ).pack(fill='x', padx=16, pady=6)
        tk.Frame(sf, bg=C['surface'], height=12).pack()

        # Right pane: log + progress
        tk.Label(right, text='PROCESSING LOG', bg=C['panel'], fg=C['text_dim'],
                 font=('Consolas', 8, 'bold')).pack(anchor='w', padx=10, pady=(10, 2))
        self.dds_log = self._console(right)
        self.dds_log.pack(fill='both', expand=True, padx=8, pady=4)
        self.dds_prog = tk.DoubleVar(value=0)
        ttk.Progressbar(right, variable=self.dds_prog, maximum=100
                        ).pack(fill='x', padx=8, pady=(0, 4))
        self._mkbtn(right, 'Clear', lambda: self._clear_log(self.dds_log),
                    bg=C['panel2'], fg=C['text_dim'], pad=(8, 3),
                    font=('Segoe UI', 8)).pack(anchor='e', padx=8, pady=(0, 8))

    # ── DDS rule management ───────────────────────────────────────────────────
    def _init_dds_rules(self):
        """Populate _dds_rule_vars from the built-in _DDS_RULES defaults."""
        self._dds_rule_vars = []
        for suffixes, fmt, _ in _DDS_RULES:
            sv = tk.StringVar(value=', '.join(suffixes))
            fv = tk.StringVar(value=fmt)
            self._dds_rule_vars.append((sv, fv))

    def _rebuild_dds_rules_ui(self):
        """Destroy and redraw every rule row inside _dds_rules_container."""
        for w in self._dds_rules_container.winfo_children():
            w.destroy()

        for idx, (sv, fv) in enumerate(self._dds_rule_vars):
            row = tk.Frame(self._dds_rules_container, bg=C['panel'])
            row.pack(fill='x', pady=2)
            row.columnconfigure(0, weight=3)
            row.columnconfigure(1, weight=4)
            row.columnconfigure(2, minsize=26)

            # Suffix entry
            tk.Entry(row, textvariable=sv, bg=C['input'], fg=C['text'],
                     insertbackground=C['text'], relief='flat',
                     font=('Segoe UI', 8)
                     ).grid(row=0, column=0, sticky='ew', padx=(6, 4), pady=5)

            # Format combobox
            cb = ttk.Combobox(row, textvariable=fv, values=_DDS_FMT_OPTIONS,
                              state='readonly', font=('Consolas', 8), width=18)
            cb.grid(row=0, column=1, sticky='ew', padx=(0, 4), pady=5)

            # Delete button
            tk.Button(row, text='×', bg=C['panel'], fg=C['error'],
                      activebackground=C['input'], activeforeground=C['error'],
                      relief='flat', cursor='hand2', font=('Segoe UI', 10, 'bold'),
                      padx=4, pady=0, bd=0,
                      command=lambda i=idx: self._remove_dds_rule(i)
                      ).grid(row=0, column=2, padx=(0, 4), pady=5)

    def _add_dds_rule(self):
        """Append a new blank rule row."""
        self._dds_rule_vars.append((tk.StringVar(value='_suffix'),
                                    tk.StringVar(value='BC7_UNORM')))
        self._rebuild_dds_rules_ui()

    def _remove_dds_rule(self, idx):
        """Remove rule at index and redraw."""
        if 0 <= idx < len(self._dds_rule_vars):
            self._dds_rule_vars.pop(idx)
            self._rebuild_dds_rules_ui()

    def _reset_dds_rules(self):
        """Restore default rules and default format."""
        self._init_dds_rules()
        self._rebuild_dds_rules_ui()
        if hasattr(self, '_dds_default_var'):
            self._dds_default_var.set(_DDS_DEFAULT[0])

    def _get_dds_format(self, filename):
        """
        Look up DDS format for filename using the current editable rules.
        Falls back to _dds_default_var if no rule matches.
        """
        stem = Path(filename).stem.lower()
        for sv, fv in self._dds_rule_vars:
            suffixes = [s.strip() for s in sv.get().split(',') if s.strip()]
            if any(stem.endswith(s) for s in suffixes):
                return fv.get()
        return getattr(self, '_dds_default_var',
                       type('', (), {'get': lambda s: _DDS_DEFAULT[0]})()
                       ).get()

    def _run_dds_tool(self, src_var, out_var, mode_var):
        if not self._need_texconv(self.dds_log): return

        # Snapshot current rules at click time (thread-safe copy)
        rule_snapshot = [(sv.get(), fv.get()) for sv, fv in self._dds_rule_vars]
        default_fmt = getattr(self, '_dds_default_var',
                               type('', (), {'get': lambda s: _DDS_DEFAULT[0]})()
                               ).get()

        def resolve_fmt(filename):
            stem = Path(filename).stem.lower()
            for suffixes_str, fmt in rule_snapshot:
                for s in [x.strip() for x in suffixes_str.split(',') if x.strip()]:
                    if stem.endswith(s):
                        return fmt
            return default_fmt

        def run():
            self._clear_log(self.dds_log); self.dds_prog.set(0)
            src = Path(src_var.get())
            out = Path(out_var.get())
            ext = '.dds' if mode_var.get() == 'dds' else '.png'
            files = [f for f in src.rglob(f'*{ext}') if f.is_file()]
            if not files:
                self._log(self.dds_log, f'No {ext.upper()} files in: {src}', C['warn']); return

            mode_label = 'DDS→DDS recompress' if mode_var.get() == 'dds' else 'PNG→DDS compress'
            self._log(self.dds_log,
                      f'Mode:   {mode_label}\n'
                      f'Source: {src}\n'
                      f'Output: {out}\n'
                      f'Rules:  {len(rule_snapshot)} custom + default={default_fmt}\n'
                      f'Found:  {len(files)} file(s)\n')
            ok = fail = 0
            for i, f in enumerate(files, 1):
                fmt = resolve_fmt(f.name)
                rel = f.relative_to(src)
                out_dir = out / rel.parent
                out_dir.mkdir(parents=True, exist_ok=True)
                dst = out_dir / (f.stem + '.dds')
                self._log(self.dds_log, f'  {rel}  →  {fmt}')
                try:
                    if _tc_compress(str(f), str(dst), fmt):
                        self._log(self.dds_log, '    ✓', C['success']); ok += 1
                    else:
                        self._log(self.dds_log, '    ✗ texconv failed', C['error']); fail += 1
                except Exception as e:
                    self._log(self.dds_log, f'    ✗ {e}', C['error']); fail += 1
                self.root.after(0, lambda p=i / len(files) * 100: self.dds_prog.set(p))

            self.dds_prog.set(100)
            self._log(self.dds_log,
                      f'\nDone — {ok} succeeded, {fail} failed.', C['success'])
        self._run_thread(run)

    # =========================================================================
    # TAB 3 – BSA UTILITIES
    # =========================================================================
    def _build_bsa_tab(self, parent):
        # Left (scrollable controls) | Right (output + log)
        left_outer = tk.Frame(parent, bg=C['surface'], width=500)
        left_outer.pack(side='left', fill='y')
        left_outer.pack_propagate(False)
        right = tk.Frame(parent, bg=C['panel'])
        right.pack(side='right', fill='both', expand=True)

        _, sf = self._make_scrollable(left_outer, C['surface'])

        # ── Archive file picker ───────────────────────────────────────────────
        self._section_lbl(sf, 'ARCHIVE FILE',
            'Select a .bsa or .ba2 file for info, list, or extraction.')

        self._bsa_file_var = tk.StringVar()
        fr = tk.Frame(sf, bg=C['surface']); fr.pack(fill='x', padx=16, pady=3)
        tk.Entry(fr, textvariable=self._bsa_file_var,
                 bg=C['input'], fg=C['text'], insertbackground=C['text'],
                 relief='flat', font=('Segoe UI', 8)
                 ).pack(side='left', fill='x', expand=True, padx=(0, 4))
        def _pick_bsa():
            f = filedialog.askopenfilename(
                title='Select BSA / BA2 archive',
                filetypes=[('Bethesda Archives', '*.bsa *.ba2'), ('All files', '*.*')])
            if f: self._bsa_file_var.set(os.path.normpath(f))
        tk.Button(fr, text=' Browse… ', command=_pick_bsa,
                  bg=C['accent'], fg='white',
                  activebackground=C['accent_hi'], activeforeground='white',
                  relief='flat', cursor='hand2', font=('Segoe UI', 8, 'bold'),
                  padx=8, pady=4, bd=0).pack(side='right')

        # ── Info buttons ──────────────────────────────────────────────────────
        self._sep(sf)
        self._section_lbl(sf, 'ARCHIVE INFO',
            'View archive metadata and file listing — output appears in the panel on the right.')

        btn_defs = [
            ('ℹ  Info',
             'Format, version,\nfile count, flags.',
             lambda: self._bsa_run_info('info')),
            ('📋  Info + List',
             'Above + full\nfile path listing.',
             lambda: self._bsa_run_info('list')),
            ('🔍  Info + Dump',
             'Above + file sizes\nand hashes.',
             lambda: self._bsa_run_info('dump')),
        ]
        info_row = tk.Frame(sf, bg=C['surface'])
        info_row.pack(fill='x', padx=16, pady=(0, 8))
        for lbl, tip, cmd in btn_defs:
            cell = tk.Frame(info_row, bg=C['panel'])
            cell.pack(side='left', fill='both', expand=True, padx=4, pady=4)
            self._mkbtn(cell, lbl, cmd,
                        pad=(4, 8), font=('Segoe UI', 9, 'bold')
                        ).pack(fill='x', padx=6, pady=(8, 2))
            tk.Label(cell, text=tip, bg=C['panel'], fg=C['text_dim'],
                     font=('Segoe UI', 7), justify='center'
                     ).pack(padx=6, pady=(0, 8))

        # ── Unpack ────────────────────────────────────────────────────────────
        self._sep(sf)
        self._section_lbl(sf, 'UNPACK',
            'Extract all files from the archive to the chosen output folder.')

        self._bsa_unpack_var = self.output_dir
        self._folder_row(sf, 'Output Folder', self._bsa_unpack_var)

        self._mkbtn(sf, '📦  Unpack Archive', self._bsa_unpack,
                    pad=(16, 9), font=('Segoe UI', 10, 'bold')
                    ).pack(fill='x', padx=16, pady=(8, 4))

        # ── Pack ─────────────────────────────────────────────────────────────
        self._sep(sf)
        self._section_lbl(sf, 'PACK',
            'Create a new BSA/BA2 from a source folder. Place files inside the folder '
            r'keeping their relative paths (e.g. source\textures\...).')

        self._bsa_src_var = tk.StringVar()
        self._folder_row(sf, 'Source Folder', self._bsa_src_var)

        # Output BSA path row
        out_row = tk.Frame(sf, bg=C['surface']); out_row.pack(fill='x', padx=16, pady=3)
        tk.Label(out_row, text='Output BSA', bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 8), width=13, anchor='w').pack(side='left')
        self._bsa_out_var = tk.StringVar()
        tk.Entry(out_row, textvariable=self._bsa_out_var,
                 bg=C['input'], fg=C['text'], insertbackground=C['text'],
                 relief='flat', font=('Segoe UI', 8)
                 ).pack(side='left', fill='x', expand=True, padx=4)
        def _pick_bsa_out():
            f = filedialog.asksaveasfilename(
                title='Save BSA as…',
                defaultextension='.bsa',
                filetypes=[('BSA Archive','*.bsa'), ('BA2 Archive','*.ba2'), ('All','*.*')])
            if f: self._bsa_out_var.set(os.path.normpath(f))
        tk.Button(out_row, text=' … ', command=_pick_bsa_out,
                  bg=C['accent'], fg='white',
                  activebackground=C['accent_hi'], activeforeground='white',
                  relief='flat', cursor='hand2', font=('Segoe UI', 9, 'bold'),
                  padx=6, bd=0).pack(side='right')

        # Format selector
        fmt_row = tk.Frame(sf, bg=C['surface']); fmt_row.pack(fill='x', padx=16, pady=(6, 2))
        tk.Label(fmt_row, text='Archive Format', bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 8), width=13, anchor='w').pack(side='left')
        self._bsa_fmt_var = tk.StringVar(value='sse')
        fmt_cb = ttk.Combobox(fmt_row, textvariable=self._bsa_fmt_var,
                              state='readonly', font=('Segoe UI', 8), width=36)
        fmt_cb['values'] = [f'-{f}  ({d})' for f, d in _BSA_PACK_FORMATS]
        fmt_cb.current(0)
        fmt_cb.pack(side='left', padx=4)

        self._mkbtn(sf, '🗜  Pack Archive', self._bsa_pack,
                    pad=(16, 9), font=('Segoe UI', 10, 'bold')
                    ).pack(fill='x', padx=16, pady=(10, 4))

        tk.Frame(sf, bg=C['surface'], height=12).pack()

        # ── Right panel ───────────────────────────────────────────────────────
        # Upper: output display + filter
        out_frame = tk.Frame(right, bg=C['panel'])
        out_frame.pack(fill='both', expand=True)

        filter_bar = tk.Frame(out_frame, bg=C['panel'])
        filter_bar.pack(fill='x', padx=8, pady=(8, 3))
        tk.Label(filter_bar, text='OUTPUT', bg=C['panel'], fg=C['text_dim'],
                 font=('Consolas', 8, 'bold')).pack(side='left')
        self._bsa_count_lbl = tk.Label(filter_bar, text='', bg=C['panel'],
                                        fg=C['text_dim'], font=('Segoe UI', 8))
        self._bsa_count_lbl.pack(side='left', padx=(6, 0))

        self._mkbtn(filter_bar, 'Clear', lambda: self._bsa_clear_output(),
                    bg=C['panel2'], fg=C['text_dim'], pad=(6, 2),
                    font=('Segoe UI', 8)).pack(side='right')
        self._bsa_filter_var = tk.StringVar()
        self._bsa_filter_var.trace_add('write', lambda *_: self._bsa_apply_filter())
        tk.Entry(filter_bar, textvariable=self._bsa_filter_var,
                 bg=C['input'], fg=C['text'], insertbackground=C['text'],
                 relief='flat', font=('Segoe UI', 8), width=22
                 ).pack(side='right', padx=(0, 4))
        tk.Label(filter_bar, text='Filter:', bg=C['panel'], fg=C['text_dim'],
                 font=('Segoe UI', 8)).pack(side='right', padx=(0, 4))

        self._bsa_output = scrolledtext.ScrolledText(
            out_frame, bg=C['con_bg'], fg='#9cdcfe',
            font=('Consolas', 8), relief='flat', state='disabled', wrap='none')
        self._bsa_output.pack(fill='both', expand=True, padx=8, pady=(0, 4))

        # Lower: progress + log
        log_frame = tk.Frame(right, bg=C['panel'], height=140)
        log_frame.pack(fill='x', side='bottom')
        log_frame.pack_propagate(False)

        lf_top = tk.Frame(log_frame, bg=C['panel']); lf_top.pack(fill='x', padx=8, pady=(5,2))
        tk.Label(lf_top, text='LOG', bg=C['panel'], fg=C['text_dim'],
                 font=('Consolas', 8, 'bold')).pack(side='left')
        self._bsa_prog = tk.DoubleVar(value=0)
        ttk.Progressbar(lf_top, variable=self._bsa_prog, maximum=100, length=180
                        ).pack(side='right')
        self.bsa_log = self._console(log_frame, height=5)
        self.bsa_log.pack(fill='both', expand=True, padx=8, pady=(0, 5))

        # Internal state
        self._bsa_all_lines = []

    # ── BSA helpers ───────────────────────────────────────────────────────────
    def _need_bsarch(self):
        if not self.bsarch_ok:
            messagebox.showerror('BSArch Missing',
                'BSArch.exe not found — it should be bundled inside this exe.\n'
                'Please rebuild using BUILD.bat to include BSArch.exe.')
            return False
        return True

    def _bsa_clear_output(self):
        self._bsa_all_lines = []
        self._bsa_output.configure(state='normal')
        self._bsa_output.delete('1.0', 'end')
        self._bsa_output.configure(state='disabled')
        self._bsa_count_lbl.config(text='')
        self._bsa_filter_var.set('')

    def _bsa_set_output(self, lines):
        """Store lines and display them (respecting current filter)."""
        self._bsa_all_lines = lines
        self._bsa_apply_filter()

    def _bsa_apply_filter(self):
        query = self._bsa_filter_var.get().lower()
        shown = [l for l in self._bsa_all_lines if query in l.lower()]                 if query else self._bsa_all_lines
        self._bsa_output.configure(state='normal')
        self._bsa_output.delete('1.0', 'end')
        self._bsa_output.insert('end', '\n'.join(shown))
        self._bsa_output.configure(state='disabled')
        total = len(self._bsa_all_lines)
        cnt   = len(shown)
        if total:
            self._bsa_count_lbl.config(
                text=f'  {cnt:,} / {total:,} lines' if query else f'  {total:,} lines')

    def _bsa_run(self, args, log_prefix, on_done=None):
        """Generic BSArch runner — captures output into the display area."""
        if not self._need_bsarch(): return

        def run():
            self._clear_log(self.bsa_log)
            self._bsa_prog.set(5)
            self._log(self.bsa_log, f'{log_prefix}…')
            try:
                result = subprocess.run(
                    _pt.bsarch_command(args),
                    capture_output=True, text=True, timeout=600)

                all_lines = (result.stdout + result.stderr).splitlines()
                self.root.after(0, lambda: self._bsa_set_output(all_lines))
                self.root.after(0, lambda: self._bsa_prog.set(100))

                rc = result.returncode
                msg = f'✓ Done  (return code {rc})' if rc == 0                       else f'⚠ BSArch returned code {rc}'
                col = C['success'] if rc == 0 else C['warn']
                self._log(self.bsa_log, msg, col)
                if on_done: self.root.after(0, on_done)
            except subprocess.TimeoutExpired:
                self._log(self.bsa_log, 'Timeout — archive may be very large.', C['warn'])
                self.root.after(0, lambda: self._bsa_prog.set(0))
            except Exception as e:
                self._log(self.bsa_log, f'Error: {e}', C['error'])
                self.root.after(0, lambda: self._bsa_prog.set(0))

        self._run_thread(run)

    def _bsa_get_file(self):
        f = self._bsa_file_var.get().strip()
        if not f or not os.path.isfile(f):
            messagebox.showerror('No Archive', 'Please select a valid .bsa or .ba2 file.')
            return None
        return f

    # ── Info / List / Dump ────────────────────────────────────────────────────
    def _bsa_run_info(self, mode):
        f = self._bsa_get_file()
        if not f: return
        label_map = {'info': 'Info', 'list': 'Info + List', 'dump': 'Info + Dump'}
        args = [f] if mode == 'info' else [f, f'-{mode}']
        self._bsa_run(args, f'{label_map[mode]}: {os.path.basename(f)}')

    # ── Unpack ────────────────────────────────────────────────────────────────
    def _bsa_unpack(self):
        f   = self._bsa_get_file()
        out = self._bsa_unpack_var.get().strip()
        if not f: return
        if not out:
            messagebox.showerror('No Output', 'Please select an output folder.'); return
        os.makedirs(out, exist_ok=True)
        name = Path(f).stem
        # BSArch unpack: bsarch unpack <archive> <output_dir>
        self._bsa_run(
            ['unpack', f, out],
            f'Unpacking {name}  →  {out}')

    # ── Pack ──────────────────────────────────────────────────────────────────
    def _bsa_pack(self):
        src = self._bsa_src_var.get().strip()
        out = self._bsa_out_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror('No Source', 'Please select a valid source folder.'); return
        if not out:
            messagebox.showerror('No Output', 'Please specify the output BSA filename.'); return

        # Parse selected format from combobox string "-sse  (Skyrim...)" → "sse"
        raw = self._bsa_fmt_var.get()
        fmt_flag = raw.split('(')[0].strip()   # e.g. "-sse"

        if not messagebox.askyesno('Confirm Pack',
            f'Pack contents of:\n  {src}\n\nInto:\n  {out}\n\nFormat: {fmt_flag}\n\nContinue?'):
            return

        self._bsa_run(
            ['pack', src, out, fmt_flag],
            f'Packing  {fmt_flag}  →  {os.path.basename(out)}')

    # ── Shared PBR/DDS folder row ─────────────────────────────────────────────
    def _folder_row(self, parent, label, var, bg=None):
        bg = bg or C['surface']
        row = tk.Frame(parent, bg=bg)
        row.pack(fill='x', padx=16, pady=3)
        tk.Label(row, text=label, bg=bg, fg=C['text'],
                 font=('Segoe UI', 8), width=13, anchor='w').pack(side='left')
        tk.Entry(row, textvariable=var, bg=C['input'], fg=C['text'],
                 insertbackground=C['text'], relief='flat', font=('Segoe UI', 8)
                 ).pack(side='left', fill='x', expand=True, padx=4)
        def _browse(v=var):
            d = filedialog.askdirectory(initialdir=v.get() or self.input_dir.get())
            if d: v.set(os.path.normpath(d))
        tk.Button(row, text=' … ', command=_browse,
                  bg=C['accent'], fg='white',
                  activebackground=C['accent_hi'], activeforeground='white',
                  relief='flat', cursor='hand2', font=('Segoe UI', 9, 'bold'), padx=6
                  ).pack(side='right')

    # =========================================================================
    # TAB 3 – NORMAL MAP GENERATOR  (BMP + DDS in, BMP/TGA/PNG/DDS out)
    # =========================================================================
    def _build_normal_maps_tab(self, parent):
        left = tk.Frame(parent, bg=C['surface'], width=420)
        left.pack(side='left', fill='y'); left.pack_propagate(False)
        right = tk.Frame(parent, bg=C['panel'])
        right.pack(side='right', fill='both', expand=True)

        self._section_lbl(left, 'NORMAL MAP SETTINGS')
        tk.Label(left,
                 text='Reads BMP and DDS from Input Folder → writes _n maps to Output Folder',
                 bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8)).pack(anchor='w', padx=16, pady=(0,10))

        # Scale
        tk.Label(left, text='Scale / Strength', bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 9)).pack(anchor='w', padx=16)
        sr = tk.Frame(left, bg=C['surface']); sr.pack(fill='x', padx=16, pady=(2,12))
        self.nm_scale = tk.DoubleVar(value=10.0)
        self._nm_scale_lbl = tk.Label(sr, text='10.0', bg=C['surface'],
                                      fg=C['text_bright'], font=('Consolas', 10,'bold'), width=5)
        self._nm_scale_lbl.pack(side='right')
        tk.Scale(sr, from_=0.5, to=50.0, resolution=0.5, orient='horizontal',
                 variable=self.nm_scale, bg=C['surface'], fg=C['text'],
                 troughcolor=C['input'], highlightthickness=0, activebackground=C['accent'],
                 showvalue=False, sliderlength=18,
                 command=lambda v: self._nm_scale_lbl.config(text=f'{float(v):.1f}')
                 ).pack(fill='x', expand=True)

        # Options
        self.nm_use_x  = tk.BooleanVar(value=True)
        self.nm_use_y  = tk.BooleanVar(value=True)
        self.nm_flip_x = tk.BooleanVar(value=False)
        self.nm_flip_y = tk.BooleanVar(value=False)
        self.nm_full_z = tk.BooleanVar(value=False)
        for lbl, var in [
            ('Include X Component (Red channel)',   self.nm_use_x),
            ('Include Y Component (Green channel)', self.nm_use_y),
            ('Flip X Direction',                    self.nm_flip_x),
            ('Flip Y Direction',                    self.nm_flip_y),
            ('Full Z Range  (enhanced contrast)',   self.nm_full_z),
        ]:
            ttk.Checkbutton(left, text=lbl, variable=var,
                            style='Dark.TCheckbutton').pack(fill='x', padx=16, pady=3)

        self.nm_use_gpu = tk.BooleanVar(value=_GPU_OK)
        gpu_cb = ttk.Checkbutton(left, text='⚡ GPU Accelerate (ModernGL compute shader)',
                        variable=self.nm_use_gpu, style='Dark.TCheckbutton')
        gpu_cb.pack(fill='x', padx=16, pady=(6,3))
        if not _GPU_OK:
            gpu_cb.state(['disabled'])
            self.nm_use_gpu.set(False)
        tk.Label(left,
                 text=('GPU: available — Sobel-based, faster on large batches' if _GPU_OK
                       else 'GPU: unavailable on this machine — using CPU'),
                 bg=C['surface'], fg=(C['success'] if _GPU_OK else C['text_dim']),
                 font=('Segoe UI', 7)).pack(anchor='w', padx=16, pady=(0,4))

        self._sep(left)

        # Output format
        tk.Label(left, text='Output Format', bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 9)).pack(anchor='w', padx=16)
        self.nm_fmt = tk.StringVar(value='BMP')
        fr = tk.Frame(left, bg=C['surface']); fr.pack(fill='x', padx=16, pady=(4,4))
        for fmt in ['BMP', 'TGA', 'PNG', 'DDS']:
            ttk.Radiobutton(fr, text=fmt, variable=self.nm_fmt, value=fmt,
                            style='Dark.TRadiobutton').pack(side='left', padx=(0,12))
        tk.Label(left,
                 text='DDS output uses BC7_UNORM (normal map linear data)',
                 bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 7)).pack(anchor='w', padx=16, pady=(0,8))

        # Input format filter
        tk.Label(left, text='Read Input Formats', bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 9)).pack(anchor='w', padx=16)
        self.nm_read_bmp = tk.BooleanVar(value=True)
        self.nm_read_dds = tk.BooleanVar(value=True)
        ifr = tk.Frame(left, bg=C['surface']); ifr.pack(fill='x', padx=16, pady=(4,8))
        ttk.Checkbutton(ifr, text='BMP', variable=self.nm_read_bmp,
                        style='Dark.TCheckbutton').pack(side='left', padx=(0,16))
        ttk.Checkbutton(ifr, text='DDS  (via texconv)', variable=self.nm_read_dds,
                        style='Dark.TCheckbutton').pack(side='left')

        self._sep(left)
        self._mkbtn(left, '▶  Generate Normal Maps', self._run_nm,
                    pad=(16,10), font=('Segoe UI', 10,'bold')).pack(fill='x', padx=16, pady=4)
        self._mkbtn(left, '🔍  Preview Last Result', self._preview_nm,
                    bg=C['panel'], fg=C['text'], pad=(16,7),
                    font=('Segoe UI', 9)).pack(fill='x', padx=16, pady=(0,4))
        self._mkbtn(left, '🗂  Open Output Folder', self._open_out,
                    bg=C['panel'], fg=C['text'], pad=(16,7),
                    font=('Segoe UI', 9)).pack(fill='x', padx=16, pady=(0,16))

        tk.Label(right, text='PROCESSING LOG', bg=C['panel'], fg=C['text_dim'],
                 font=('Consolas', 8,'bold')).pack(anchor='w', padx=10, pady=(10,2))
        self.nm_log = self._console(right)
        self.nm_log.pack(fill='both', expand=True, padx=8, pady=(0,4))
        self._mkbtn(right, 'Clear', lambda: self._clear_log(self.nm_log),
                    bg=C['panel2'], fg=C['text_dim'], pad=(8,3),
                    font=('Segoe UI', 8)).pack(anchor='e', padx=8, pady=(0,8))

    def _run_nm(self):
        def r():
            self._clear_log(self.nm_log)
            scale  = self.nm_scale.get()
            use_x  = self.nm_use_x.get()
            use_y  = self.nm_use_y.get()
            flip_x = self.nm_flip_x.get()
            flip_y = self.nm_flip_y.get()
            full_z = self.nm_full_z.get()
            use_gpu = self.nm_use_gpu.get()
            fmt    = self.nm_fmt.get().lower()
            read_bmp = self.nm_read_bmp.get()
            read_dds = self.nm_read_dds.get()

            if fmt == 'dds' and not self.texconv_ok:
                self._log(self.nm_log, 'DDS output requires texconv.', C['error']); return
            if read_dds and not self.texconv_ok:
                self._log(self.nm_log,
                    'DDS input requires texconv — disabling DDS input.', C['warn'])
                read_dds = False

            exts = (['.bmp'] if read_bmp else []) + (['.dds'] if read_dds else [])
            if not exts:
                self._log(self.nm_log, 'No input formats selected.', C['warn']); return

            self._log(self.nm_log,
                      f'Scale={scale}  X={use_x}  Y={use_y}  flipX={flip_x}  '
                      f'flipY={flip_y}  fullZ={full_z}  fmt={fmt.upper()}\n'
                      f'Reading: {", ".join(e.upper() for e in exts)}\n'
                      f'Input:  {self.input_dir.get()}\n'
                      f'Output: {self.output_dir.get()}\n')

            all_files = self._find(*[e.lstrip('.') for e in exts])
            files = [f for f in all_files if '_n.' not in os.path.basename(f).lower()]

            if not files:
                self._log(self.nm_log, 'No eligible files found.', C['warn']); return
            self._log(self.nm_log, f'Found {len(files)} file(s)…\n')
            if use_gpu and _GPU_OK:
                self._log(self.nm_log, 'GPU acceleration ON (ModernGL compute shader)\n', C['success'])
            gpu_fallback_warned = False

            ok_count = 0
            for f in files:
                rel = os.path.relpath(f, self.input_dir.get())
                self._log(self.nm_log, f'Processing: {rel}')
                try:
                    # Load: DDS needs extraction first
                    if f.lower().endswith('.dds'):
                        with tempfile.TemporaryDirectory() as tmp:
                            png = _tc_extract(f, tmp)
                            if not png:
                                raise RuntimeError('texconv extraction failed')
                            source_img = Image.open(png)
                            nm, backend = generate_normal_map_auto(source_img, scale, use_x, use_y,
                                                     flip_x, flip_y, full_z, use_gpu)
                    else:
                        nm, backend = generate_normal_map_auto(Image.open(f), scale, use_x, use_y,
                                                 flip_x, flip_y, full_z, use_gpu)
                    if use_gpu and backend == 'cpu' and not gpu_fallback_warned:
                        self._log(self.nm_log, '  ⚠ GPU render failed, fell back to CPU for this batch', C['warn'])
                        gpu_fallback_warned = True

                    # Save output
                    out_base = self._mirror_out(f, 'png' if fmt == 'dds' else fmt)
                    stem, ext_part = os.path.splitext(out_base)
                    out_img = f'{stem}_n{ext_part}'

                    if fmt == 'dds':
                        # Save PNG first, then compress
                        nm.save(out_img)
                        dds_out = out_img.replace('.png', '.dds')
                        if _tc_compress(out_img, dds_out, 'BC7_UNORM'):
                            os.remove(out_img)
                            out_img = dds_out
                        else:
                            self._log(self.nm_log, '  ⚠ DDS compress failed, kept PNG', C['warn'])
                    else:
                        nm.save(out_img)

                    self.last_nm_path = out_img
                    self._log(self.nm_log,
                              f'  ✓ → {os.path.relpath(out_img, self.output_dir.get())}',
                              C['success'])
                    ok_count += 1
                except Exception as e:
                    self._log(self.nm_log, f'  ✗ {e}', C['error'])

            self._log(self.nm_log,
                      f'\n✓ Complete — {ok_count}/{len(files)} processed.', C['success'])
        self._run_thread(r)

    def _preview_nm(self):
        path = getattr(self, 'last_nm_path', None)
        if not path or not os.path.exists(path):
            path = filedialog.askopenfilename(
                initialdir=self.output_dir.get(),
                title='Select image to preview',
                filetypes=[('Images','*.bmp *.tga *.png'), ('All files','*.*')])
        if not path: return
        try:
            img = Image.open(path)
            t = img.copy(); t.thumbnail((512,512), Image.LANCZOS)
            photo = ImageTk.PhotoImage(t)
            win = tk.Toplevel(self.root)
            win.title(f'Preview — {os.path.basename(path)}')
            win.configure(bg=C['bg']); win.resizable(False,False)
            tk.Label(win, image=photo, bg=C['bg']).pack(padx=10, pady=10)
            tk.Label(win, text=f'{os.path.basename(path)}  |  {img.width}×{img.height}  |  {img.mode}',
                     bg=C['bg'], fg=C['text_dim'], font=('Segoe UI',8)).pack(pady=(0,10))
            win._photo = photo
        except Exception as e:
            messagebox.showerror('Preview Error', str(e))

    def _open_out(self):
        d = self.output_dir.get()
        if os.path.isdir(d):
            try: _pt.open_in_file_manager(d)
            except Exception: subprocess.Popen(['explorer', d])

    # =========================================================================
    # TAB 4 – PBR GENERATION
    # =========================================================================
    def _build_pbr_tab(self, parent):
        if not _PBR_OK:
            tk.Label(parent,
                     text='pbr_engine.py not found — place it alongside this executable.',
                     bg=C['surface'], fg=C['error'],
                     font=('Segoe UI', 10)).pack(expand=True)
            return
        nb2 = ttk.Notebook(parent); nb2.pack(fill='both', expand=True)
        for label, fn in [
            ('PBR Builder',        self._pbr_builder_tab),
            ('Parallax Generator', self._pbr_parallax_tab),
            ('Complex → PBR',      self._pbr_conv_to_pbr_tab),
            ('PBR → Complex',      self._pbr_conv_to_complex_tab),
        ]:
            tab = tk.Frame(nb2, bg=C['surface'])
            nb2.add(tab, text=f'  {label}  ')
            fn(tab)

    # =========================================================================
    # TAB – PBR JSON BUILDERS (split: engine-native builder | Step1/Step2 port)
    # =========================================================================
    def _build_pbr_json_split_tab(self, parent):
        left_half = tk.Frame(parent, bg=C['surface'])
        left_half.pack(side='left', fill='both', expand=True)
        sep = tk.Frame(parent, bg=C['border'], width=2)
        sep.pack(side='left', fill='y')
        right_half = tk.Frame(parent, bg=C['surface'])
        right_half.pack(side='left', fill='both', expand=True)

        if not _PBR_OK:
            tk.Label(parent,
                     text='pbr_engine.py not found — place it alongside this executable.',
                     bg=C['surface'], fg=C['error'],
                     font=('Segoe UI', 10)).pack(expand=True)
            return

        self._pbr_json_tab(left_half)        # existing combined-JSON builder
        self._pbr_json_ps1_tab(right_half)    # Step1.ps1 + Step2.ps1 port (one JSON per texture)

    def _pbr_layout(self, parent, title, desc):
        left = tk.Frame(parent, bg=C['surface'], width=440)
        left.pack(side='left', fill='y'); left.pack_propagate(False)
        right = tk.Frame(parent, bg=C['panel'])
        right.pack(side='right', fill='both', expand=True)
        tk.Label(left, text=title, bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 10,'bold')).pack(anchor='w', padx=16, pady=(14,1))
        tk.Label(left, text=desc, bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8), wraplength=390, justify='left'
                 ).pack(anchor='w', padx=16, pady=(0,10))
        tk.Label(right, text='PROCESSING LOG', bg=C['panel'], fg=C['text_dim'],
                 font=('Consolas', 8,'bold')).pack(anchor='w', padx=10, pady=(10,2))
        log_w = self._console(right)
        log_w.pack(fill='both', expand=True, padx=8, pady=4)
        pv = tk.DoubleVar(value=0)
        ttk.Progressbar(right, variable=pv, maximum=100).pack(fill='x', padx=8, pady=(0,4))
        self._mkbtn(right, 'Clear', lambda: self._clear_log(log_w),
                    bg=C['panel2'], fg=C['text_dim'], pad=(8,3),
                    font=('Segoe UI', 8)).pack(anchor='e', padx=8, pady=(0,8))
        return left, log_w, pv

    def _slider_row(self, parent, label, lo, hi, res, default, w=14):
        row = tk.Frame(parent, bg=C['surface']); row.pack(fill='x', padx=16, pady=3)
        tk.Label(row, text=label, bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 8), width=w, anchor='w').pack(side='left')
        var = tk.DoubleVar(value=default)
        lbl = tk.Label(row, text=str(default), bg=C['surface'],
                       fg=C['text_bright'], font=('Consolas', 8), width=6)
        lbl.pack(side='right')
        tk.Scale(row, from_=lo, to=hi, resolution=res, orient='horizontal',
                 variable=var, bg=C['surface'], fg=C['text'],
                 troughcolor=C['input'], highlightthickness=0, showvalue=False, sliderlength=14,
                 command=lambda v, l=lbl: l.config(text=f'{float(v):.3f}')
                 ).pack(fill='x', expand=True, padx=(0,4))
        return var

    def _run_pbr_op(self, fn, log_w, pv, *args, done_msg='Done.', **kwargs):
        def _log(msg, c=None):
            fg = {'success':C['success'],'warn':C['warn'],'error':C['error']}.get(c)
            self.root.after(0, lambda: self._log(log_w, msg, fg))
        def _prog(done, total):
            self.root.after(0, lambda: pv.set(done/total*100 if total else 0))
        cancelled = {'v': False}
        def run():
            self._clear_log(log_w); pv.set(0)
            try:
                result = fn(*args, log=_log, progress=_prog,
                            cancelled=lambda: cancelled['v'], **kwargs)
                self.root.after(0, lambda: pv.set(100))
                self.root.after(0, lambda: self._log(log_w,
                    f'\n{done_msg}  ({result})', C['success']))
            except Exception as e:
                self.root.after(0, lambda: self._log(log_w, f'\nError: {e}', C['error']))
        threading.Thread(target=run, daemon=True).start()

    def _chk_tc(self):
        if not self.texconv_ok:
            messagebox.showerror('texconv Missing',
                'texconv.exe not found. Rebuild to bundle it inside the exe.')
            return False
        return True

    def _pbr_builder_tab(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'PBR BUILDER',
            'Convert loose PBR maps (albedo, normal, roughness, metalness, AO, height) '
            'into packed Community Shaders DDS files.')
        src_var = self.input_dir
        out_var = self.output_dir
        self._folder_row(left, 'Source Folder', src_var)
        self._folder_row(left, 'Output Folder', out_var)
        self._sep(left)
        tk.Label(left, text='OPTIONS', bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 9,'bold')).pack(anchor='w', padx=16)
        flip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text='Flip Normal Green Channel (Y-flip for DirectX normals)',
                        variable=flip_var, style='Dark.TCheckbutton').pack(anchor='w', padx=16, pady=6)
        tk.Label(left,
                 text='Textures matched by name keywords:\n'
                      '  albedo/diffuse/basecolor  ·  normal/normalgl  ·  roughness/rough\n'
                      '  metallic/metalness  ·  ao/occlusion  ·  height/displacement',
                 bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8), justify='left').pack(anchor='w', padx=16, pady=(4,8))
        self._sep(left)
        self._mkbtn(left, '▶  Build PBR Textures',
                    lambda: self._chk_tc() and self._run_pbr_op(
                        _pe.run_build_pbr, log_w, pv,
                        src_var.get(), out_var.get(), flip_var.get(),
                        done_msg='PBR Builder complete.'),
                    pad=(16,10), font=('Segoe UI', 10,'bold')).pack(fill='x', padx=16, pady=6)

    def _pbr_parallax_tab(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'PARALLAX GENERATOR',
            'Generate Complex Parallax _m and/or CS PBR textures from diffuse + normal. '
            'Height is derived via FFT if not present in source.')
        src_var = self.input_dir
        out_var = self.output_dir
        self._folder_row(left, 'Source Folder', src_var)
        self._folder_row(left, 'Output Folder', out_var)
        self._sep(left)
        tk.Label(left, text='OUTPUT MODE', bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 9,'bold')).pack(anchor='w', padx=16)
        mode_var = tk.StringVar(value='both')
        for val, lbl in [('complex','Complex Parallax only'),
                         ('pbr','Community Shaders PBR only'),
                         ('both','Both  (recommended)')]:
            ttk.Radiobutton(left, text=lbl, variable=mode_var, value=val,
                            style='Dark.TRadiobutton').pack(anchor='w', padx=22, pady=2)
        self._sep(left)
        tk.Label(left, text='HEIGHT GENERATION', bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 9,'bold')).pack(anchor='w', padx=16)
        sliders = {
            'contrast_factor': self._slider_row(left, 'Contrast',    0.0, 2.0,  0.05, 0.4),
            'clamp_low':       self._slider_row(left, 'Clamp Low',   0,   128,  1,    60),
            'clamp_high':      self._slider_row(left, 'Clamp High',  128, 255,  1,    200),
            'blur_radius':     self._slider_row(left, 'Blur Radius', 0,   12,   0.5,  4.0),
        }
        er = tk.Frame(left, bg=C['surface']); er.pack(fill='x', padx=16, pady=(8,2))
        tk.Label(er, text='Exclude keywords:', bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8)).pack(side='left', padx=(0,8))
        excl_var = tk.StringVar()
        tk.Entry(er, textvariable=excl_var, bg=C['input'], fg=C['text'],
                 insertbackground=C['text'], relief='flat', font=('Segoe UI', 8)
                 ).pack(side='left', fill='x', expand=True)
        self._sep(left)
        def run():
            if not self._chk_tc(): return
            excl = [x.strip() for x in excl_var.get().split(',') if x.strip()]
            cfg  = {'default': {k: v.get() for k, v in sliders.items()}, 'exclude': excl}
            self._run_pbr_op(_pe.run_generate_parallax, log_w, pv,
                             src_var.get(), out_var.get(), mode_var.get(),
                             config_override=cfg, done_msg='Parallax generation complete.')
        self._mkbtn(left, '▶  Generate Parallax Textures', run,
                    pad=(16,10), font=('Segoe UI', 10,'bold')).pack(fill='x', padx=16, pady=6)

    def _pbr_json_tab(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'PBR JSON BUILDER',
            'Scan mod folder for complete PBR sets (diffuse + normal + _p + _rmaos) '
            'and write a PBRNIFPatcher JSON to <mod>/PBRNIFPatcher/<name>.json')
        mod_var  = tk.StringVar(value=self.input_dir.get())
        name_var = tk.StringVar(value='my_mod_pbr')
        self._folder_row(left, 'Mod Folder', mod_var)
        nr = tk.Frame(left, bg=C['surface']); nr.pack(fill='x', padx=16, pady=3)
        tk.Label(nr, text='JSON Name', bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 8), width=13, anchor='w').pack(side='left')
        tk.Entry(nr, textvariable=name_var, bg=C['input'], fg=C['text'],
                 insertbackground=C['text'], relief='flat', font=('Segoe UI', 8)
                 ).pack(side='left', fill='x', expand=True, padx=4)
        self._sep(left)
        tk.Label(left, text='DEFAULT SETTINGS', bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 9,'bold')).pack(anchor='w', padx=16)
        s_vars = {
            'displacement_scale': self._slider_row(left, 'Displacement Scale', 0.0,2.0,0.05,0.4,18),
            'specular_level':     self._slider_row(left, 'Specular Level',     0.0,1.0,0.01,0.02,18),
            'roughness_scale':    self._slider_row(left, 'Roughness Scale',    0.0,2.0,0.05,1.0,18),
            'smooth_angle':       self._slider_row(left, 'Smooth Angle',       0,180,1,75,18),
        }
        b_vars = {}
        for key, lbl, dv in [('parallax','Enable Parallax',True),
                              ('emissive','Enable Emissive',False),
                              ('subsurface','Enable Subsurface',False)]:
            v = tk.BooleanVar(value=dv)
            ttk.Checkbutton(left, text=lbl, variable=v,
                            style='Dark.TCheckbutton').pack(anchor='w', padx=16, pady=2)
            b_vars[key] = v
        self._sep(left)
        def run():
            cfg = {'defaults': {**{k: v.get() for k,v in s_vars.items()},
                                **{k: v.get() for k,v in b_vars.items()}}}
            self._run_pbr_op(_pe.run_generate_json, log_w, pv,
                             mod_var.get(), name_var.get(),
                             config_override=cfg, done_msg='JSON generation complete.')
        self._mkbtn(left, '▶  Generate PBR JSON', run,
                    pad=(16,10), font=('Segoe UI', 10,'bold')).pack(fill='x', padx=16, pady=6)

    def _pbr_json_ps1_tab(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'PBR JSON BUILDER (Step1/Step2)',
            'Python port of the Step1.ps1 + Step2.ps1 pipeline: writes one JSON '
            'per diffuse texture under <mod>/Textures/PBR, mirroring the folder '
            'layout into <mod>/PBRNifPatcher. Only a diffuse image is required; '
            'other maps are auto-detected from sibling files.')
        mod_var = tk.StringVar(value=self.input_dir.get())
        self._folder_row(left, 'Mod Folder', mod_var)
        tr = tk.Frame(left, bg=C['surface']); tr.pack(fill='x', padx=16, pady=3)
        tk.Label(tr, text='Textures Subdir', bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 8), width=13, anchor='w').pack(side='left')
        tex_sub_var = tk.StringVar(value='Textures/PBR')
        tk.Entry(tr, textvariable=tex_sub_var, bg=C['input'], fg=C['text'],
                 insertbackground=C['text'], relief='flat', font=('Segoe UI', 8)
                 ).pack(side='left', fill='x', expand=True, padx=4)
        outr = tk.Frame(left, bg=C['surface']); outr.pack(fill='x', padx=16, pady=3)
        tk.Label(outr, text='Output Subdir', bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 8), width=13, anchor='w').pack(side='left')
        out_sub_var = tk.StringVar(value='PBRNifPatcher')
        tk.Entry(outr, textvariable=out_sub_var, bg=C['input'], fg=C['text'],
                 insertbackground=C['text'], relief='flat', font=('Segoe UI', 8)
                 ).pack(side='left', fill='x', expand=True, padx=4)
        self._sep(left)
        tk.Label(left, text="Renames any '_d'-suffixed diffuse file on disk once "
                 "its JSON is written (matches Step2.ps1's behavior).",
                 bg=C['surface'], fg=C['text_dim'], font=('Segoe UI', 8),
                 wraplength=390, justify='left').pack(anchor='w', padx=16, pady=(0,8))
        def run():
            self._run_pbr_op(_pe.run_generate_json_ps1_style, log_w, pv,
                             mod_var.get(),
                             textures_subdir=tex_sub_var.get(),
                             output_subdir=out_sub_var.get(),
                             done_msg='JSON generation complete.')
        self._mkbtn(left, '▶  Generate PBR JSON (Step1/Step2)', run,
                    pad=(16,10), font=('Segoe UI', 10,'bold')).pack(fill='x', padx=16, pady=6)

    def _pbr_conv_to_pbr_tab(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'COMPLEX PARALLAX → CS PBR',
            'Convert Complex Parallax sets (_m) to Community Shaders PBR format.\n'
            'Needs: <name>.dds  +  <name>_n.dds  +  <name>_m.dds')
        src_var = self.input_dir
        out_var = self.output_dir
        self._folder_row(left, 'Source Folder', src_var)
        self._folder_row(left, 'Output Folder', out_var)
        self._sep(left)
        tk.Label(left,
                 text='Output: <output>/textures/PBR/<original_path>/\n\n'
                      'Generated:  <name>_rmaos.dds  ·  <name>_p.dds',
                 bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8), justify='left').pack(anchor='w', padx=16, pady=4)
        self._sep(left)
        self._mkbtn(left, '▶  Convert to PBR',
                    lambda: self._chk_tc() and self._run_pbr_op(
                        _pe.run_convert_to_pbr, log_w, pv,
                        src_var.get(), out_var.get(),
                        done_msg='Complex → PBR complete.'),
                    pad=(16,10), font=('Segoe UI', 10,'bold')).pack(fill='x', padx=16, pady=6)

    def _pbr_conv_to_complex_tab(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'CS PBR → COMPLEX PARALLAX',
            'Convert CS PBR texture sets to Complex Parallax _m format.\n'
            'Needs: diffuse  +  _n normal  +  _p height  (optional: _rmaos)')
        src_var = self.input_dir
        out_var = self.output_dir
        self._folder_row(left, 'Source Folder', src_var)
        self._folder_row(left, 'Output Folder', out_var)
        self._sep(left)
        tk.Label(left,
                 text='_m channel packing:\n'
                      '  R / B  —  metalness (from _rmaos if present)\n'
                      '  G      —  brightness-adjusted from diffuse green\n'
                      '  A      —  height (from _p)',
                 bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8), justify='left').pack(anchor='w', padx=16, pady=4)
        self._sep(left)
        self._mkbtn(left, '▶  Convert to Complex Parallax',
                    lambda: self._chk_tc() and self._run_pbr_op(
                        _pe.run_convert_to_complex, log_w, pv,
                        src_var.get(), out_var.get(),
                        done_msg='PBR → Complex complete.'),
                    pad=(16,10), font=('Segoe UI', 10,'bold')).pack(fill='x', padx=16, pady=6)


    # MATERIAL GENERATOR TAB
    # =========================================================================
    def _build_material_tab(self, parent):
        """Generate full PBR map set from a single diffuse texture."""
        self._mat_cells  = {}
        self._mat_photos = {}
        self._mat_paths  = {}
        self._mat_enabled  = {}
        self._mat_svars    = {}   # {map_name: {param: DoubleVar/BooleanVar}}

        # Left scrollable controls | Right preview grid
        left_outer = tk.Frame(parent, bg=C['surface'], width=490)
        left_outer.pack(side='left', fill='y')
        left_outer.pack_propagate(False)
        right = tk.Frame(parent, bg=C['bg'])
        right.pack(side='right', fill='both', expand=True)

        _, sf = self._make_scrollable(left_outer, C['surface'])

        # ── Source ────────────────────────────────────────────────────────────
        self._section_lbl(sf, 'SOURCE TEXTURE',
            'Load a diffuse / albedo image to generate all PBR maps from it.')

        self._mat_src_var = tk.StringVar()
        src_r = tk.Frame(sf, bg=C['surface'])
        src_r.pack(fill='x', padx=16, pady=3)
        tk.Entry(src_r, textvariable=self._mat_src_var,
                 bg=C['input'], fg=C['text'], insertbackground=C['text'],
                 relief='flat', font=('Segoe UI', 8)
                 ).pack(side='left', fill='x', expand=True, padx=(0, 4))
        def _pick():
            f = filedialog.askopenfilename(
                title='Select diffuse / albedo texture',
                filetypes=[('Images', '*.png *.jpg *.jpeg *.bmp *.tga *.tif *.dds'),
                           ('All files', '*.*')])
            if f:
                self._mat_src_var.set(os.path.normpath(f))
                self._mat_load_preview()
        tk.Button(src_r, text=' Browse… ', command=_pick,
                  bg=C['accent'], fg='white',
                  activebackground=C['accent_hi'], activeforeground='white',
                  relief='flat', cursor='hand2', font=('Segoe UI', 8, 'bold'),
                  padx=8, pady=4, bd=0).pack(side='right')

        self._mkbtn(sf, '🖼  Load & Preview Source', self._mat_load_preview,
                    bg=C['panel'], fg=C['text'], pad=(16, 6),
                    font=('Segoe UI', 9)).pack(fill='x', padx=16, pady=4)

        # ── Source adjustments (feeds into every derived map) ────────────────
        self._sep(sf)
        self._section_lbl(sf, 'SOURCE ADJUSTMENTS',
            'Applied to the diffuse image before every map below is derived from it.')
        self._mat_live_var = tk.BooleanVar(value=True)
        live_row = tk.Frame(sf, bg=C['surface'])
        live_row.pack(fill='x', padx=16, pady=(0, 4))
        ttk.Checkbutton(live_row, text='Live preview (update thumbnails while dragging sliders)',
                        variable=self._mat_live_var,
                        style='Dark.TCheckbutton').pack(anchor='w')
        diffuse_card = tk.Frame(sf, bg=C['panel'])
        diffuse_card.pack(fill='x', padx=16, pady=3)
        tk.Frame(diffuse_card, bg=C['panel'], height=4).pack()
        self._mat_svars['diffuse'] = {}
        for s_lbl, s_key, lo, hi, res, default in [
            ('Brightness', 'brightness', -0.5, 0.5, 0.05, 0.0),
            ('Contrast',   'contrast',    0.1, 3.0, 0.05, 1.0),
            ('Saturation', 'saturation',  0.0, 2.0, 0.05, 1.0),
        ]:
            sv = self._mat_compact_slider(diffuse_card, s_lbl, lo, hi, res, default,
                    on_change=lambda v: self._mat_schedule_live('diffuse'))
            self._mat_svars['diffuse'][s_key] = sv
        tk.Frame(diffuse_card, bg=C['panel'], height=5).pack()

        # ── Output ────────────────────────────────────────────────────────────
        self._sep(sf)
        self._section_lbl(sf, 'OUTPUT')
        self._mat_out_var = self.output_dir
        self._folder_row(sf, 'Output Folder', self._mat_out_var)

        fmt_r = tk.Frame(sf, bg=C['surface'])
        fmt_r.pack(fill='x', padx=16, pady=(4, 2))
        tk.Label(fmt_r, text='Format:', bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 8)).pack(side='left', padx=(0, 8))
        self._mat_fmt_var = tk.StringVar(value='PNG')
        for fmt in ['PNG', 'TGA', 'BMP']:
            ttk.Radiobutton(fmt_r, text=fmt, variable=self._mat_fmt_var, value=fmt,
                            style='Dark.TRadiobutton').pack(side='left', padx=(0, 10))

        # ── Map settings ──────────────────────────────────────────────────────
        self._sep(sf)
        self._section_lbl(sf, 'MAP SETTINGS',
            'Enable / disable maps and adjust parameters.')

        map_defs = [
            ('height',    'HEIGHT',    '#4ec9b0',
             [('Blur',       'blur_radius',  0.0, 10.0, 0.5,  2.0),
              ('Contrast',   'contrast',     0.1,  4.0, 0.1,  1.0),
              ('Brightness', 'brightness',  -0.5,  0.5, 0.05, 0.0)],
             [('Invert', 'invert')]),

            ('normal',    'NORMAL',    '#569cd6',
             [('Scale',        'scale',          0.5, 30.0, 0.5,  8.0),
              ('Diffuse Wgt.', 'diffuse_weight', 0.0,  1.0, 0.05, 0.0)],
             [('Flip X', 'flip_x'), ('Flip Y', 'flip_y'), ('Full Z', 'full_z')]),

            ('ao',        'AO',        '#9cdcfe',
             [('Pixel Spread', 'spread', 1.0, 16.0, 0.5, 4.0),
              ('Pixel Depth',  'depth',  1.0, 16.0, 0.5, 4.0),
              ('Blend N/D',    'blend',  0.0,  1.0, 0.05,1.0),
              ('Power',        'power',  0.25, 5.0, 0.25,1.0),
              ('Bias',         'bias',  -0.5,  0.5, 0.05,0.0)],
             []),

            ('roughness', 'ROUGHNESS', '#ce9178',
             [('Base',      'base_roughness', 0.0, 1.0, 0.05, 0.65),
              ('Lum Infl.', 'lum_influence',  0.0, 1.0, 0.05, 0.40),
              ('Sat Infl.', 'sat_influence',  0.0, 1.0, 0.05, 0.25),
              ('Blur',      'blur_radius',    0.0, 8.0, 0.5,  1.5)],
             [('→ Smooth', 'invert')]),

            ('metalness', 'METALNESS', '#dcdcaa',
             [('Threshold', 'threshold',  0.0, 1.0, 0.05, 0.55),
              ('Sharpness', 'sharpness',  1.0,12.0, 0.5,  6.0),
              ('Blur',      'blur_radius',0.0, 8.0, 0.5,  2.0)],
             []),

            ('edge',      'EDGE',      '#c586c0',
             [('Blur',     'blur_radius',    0.0,  5.0, 0.5,  1.0),
              ('Low Thr',  'threshold_low',  0,   100,  5,   40),
              ('High Thr', 'threshold_high', 50,  255,  5,  130),
              ('Dilate',   'dilate',         0,    8,   1,    1),
              ('Soften',   'soften',         0.0,  4.0, 0.25, 0.5)],
             [('Invert', 'invert')]),

            ('emissive',  'EMISSIVE',  '#ff8c00',
             [('Threshold',   'threshold',   0.4, 1.0, 0.05, 0.80),
              ('Falloff',     'falloff',     0.01, 0.5, 0.01, 0.12),
              ('Bloom',       'bloom_radius',0.0,  8.0, 0.5,  2.0)],
             []),
        ]

        for map_name, label, col, sliders, toggles in map_defs:
            self._mat_svars[map_name] = {}
            card = tk.Frame(sf, bg=C['panel'])
            card.pack(fill='x', padx=16, pady=3)

            # Header: enable checkbox + name + toggles
            hdr = tk.Frame(card, bg=C['panel'])
            hdr.pack(fill='x', padx=8, pady=(6, 2))
            en = tk.BooleanVar(value=True)
            self._mat_enabled[map_name] = en
            ttk.Checkbutton(hdr, text='', variable=en,
                            style='Panel.TCheckbutton',
                            command=lambda m=map_name: self._mat_schedule_live(m)
                            ).pack(side='left')
            tk.Label(hdr, text=label, bg=C['panel'], fg=col,
                     font=('Segoe UI', 9, 'bold')).pack(side='left', padx=(2, 8))
            for tog_lbl, tog_key in toggles:
                tv = tk.BooleanVar(value=False)
                ttk.Checkbutton(hdr, text=tog_lbl, variable=tv,
                                style='Panel.TCheckbutton',
                                command=lambda m=map_name: self._mat_schedule_live(m)
                                ).pack(side='right', padx=(4, 0))
                self._mat_svars[map_name][tog_key] = tv

            # Sliders
            for s_lbl, s_key, lo, hi, res, default in sliders:
                sv = self._mat_compact_slider(card, s_lbl, lo, hi, res, default,
                        on_change=lambda v, m=map_name: self._mat_schedule_live(m))
                self._mat_svars[map_name][s_key] = sv

            tk.Frame(card, bg=C['panel'], height=5).pack()

        # ── RMAOS packer ─────────────────────────────────────────────────────────
        self._sep(sf)
        self._section_lbl(sf, 'RMAOS CHANNEL PACKER',
            'Pack generated maps into a single combined RMAOS texture.\n'
            'R = Roughness  ·  G = Metalness  ·  B = AO  ·  A = Specular')

        rmaos_card = tk.Frame(sf, bg=C['panel'])
        rmaos_card.pack(fill='x', padx=16, pady=3)

        rmaos_hdr = tk.Frame(rmaos_card, bg=C['panel'])
        rmaos_hdr.pack(fill='x', padx=8, pady=(6, 4))
        rmaos_en = tk.BooleanVar(value=True)
        self._mat_enabled['rmaos'] = rmaos_en
        ttk.Checkbutton(rmaos_hdr, text='', variable=rmaos_en,
                        style='Panel.TCheckbutton').pack(side='left')
        tk.Label(rmaos_hdr, text='RMAOS', bg=C['panel'], fg='#4ec9b0',
                 font=('Segoe UI', 9, 'bold')).pack(side='left', padx=(2, 0))
        tk.Label(rmaos_hdr, text='  auto-packed after generation',
                 bg=C['panel'], fg=C['text_dim'],
                 font=('Segoe UI', 7)).pack(side='left', padx=(6, 0))

        import material_engine as _me_local
        src_opts = _me_local.RMAOS_CHANNEL_SOURCES

        self._mat_svars['rmaos'] = {}
        ch_defs = [
            ('R  Roughness', 'r_src', 'roughness',  '#ce9178'),
            ('G  Metalness', 'g_src', 'metalness',  '#dcdcaa'),
            ('B  AO',        'b_src', 'ao',          '#9cdcfe'),
            ('A  Specular',  'a_src', 'smoothness',  '#c8c8c8'),
        ]
        for ch_lbl, ch_key, ch_default, ch_col in ch_defs:
            row = tk.Frame(rmaos_card, bg=C['panel'])
            row.pack(fill='x', padx=8, pady=2)
            tk.Label(row, text=ch_lbl, bg=C['panel'], fg=ch_col,
                     font=('Segoe UI', 8, 'bold'), width=14, anchor='w'
                     ).pack(side='left')
            v = tk.StringVar(value=ch_default)
            self._mat_svars['rmaos'][ch_key] = v
            cb = ttk.Combobox(row, textvariable=v, values=src_opts,
                              state='readonly', font=('Segoe UI', 8), width=16)
            cb.pack(side='left', padx=(4, 0))

        tk.Frame(rmaos_card, bg=C['panel'], height=4).pack()

        self._mkbtn(rmaos_card, '🗜  Pack RMAOS from Current Maps',
                    self._mat_pack_rmaos,
                    bg=C['accent'], fg='white', pad=(12, 7),
                    font=('Segoe UI', 9, 'bold')
                    ).pack(fill='x', padx=8, pady=(0, 8))

        # ── Preview launcher ─────────────────────────────────────────────────────
        self._sep(sf)
        self._section_lbl(sf, 'MATERIAL PREVIEW',
            '3D real-time preview with full PBR shading — drag to rotate.')

        prev_card = tk.Frame(sf, bg=C['panel'])
        prev_card.pack(fill='x', padx=16, pady=3)
        tk.Label(prev_card,
                 text='Opens a floating preview window with Sphere / Cube / Cylinder / Plane,\n'
                      'Cook-Torrance PBR shading, normal mapping, parallax, and full\n'
                      'material controls. Drag the render to orbit. Apply multipliers\n'
                      'directly back to the RMAOS texture from inside the preview.',
                 bg=C['panel'], fg=C['text_dim'],
                 font=('Segoe UI', 8), justify='left').pack(anchor='w', padx=10, pady=(8,4))
        self._mkbtn(prev_card, '🔭  Open Material Preview',
                    self._open_material_preview,
                    bg='#1a3a2a', fg=C['success'],
                    pad=(14, 10), font=('Segoe UI', 10, 'bold')
                    ).pack(fill='x', padx=10, pady=(0, 10))

        # ── Generate ──────────────────────────────────────────────────────────
        self._sep(sf)
        self._mkbtn(sf, '▶  Generate All Maps', self._mat_generate,
                    pad=(16, 12), font=('Segoe UI', 11, 'bold')
                    ).pack(fill='x', padx=16, pady=6)

        self._mat_prog = tk.DoubleVar(value=0)
        ttk.Progressbar(sf, variable=self._mat_prog, maximum=100
                        ).pack(fill='x', padx=16, pady=(0, 4))
        self._mat_status = tk.Label(sf, text='Ready.', bg=C['surface'],
                                     fg=C['text_dim'], font=('Segoe UI', 8),
                                     wraplength=430, justify='left')
        self._mat_status.pack(anchor='w', padx=16, pady=(0, 14))

        # ── Right: 2-column preview grid ──────────────────────────────────────
        rc = tk.Canvas(right, bg=C['bg'], highlightthickness=0)
        rsb = ttk.Scrollbar(right, orient='vertical', command=rc.yview)
        gf = tk.Frame(rc, bg=C['bg'])
        gf.bind('<Configure>', lambda e: rc.configure(scrollregion=rc.bbox('all')))
        gw = rc.create_window((0, 0), window=gf, anchor='nw')
        rc.configure(yscrollcommand=rsb.set)
        rc.bind('<Configure>', lambda e, w=gw: rc.itemconfig(w, width=e.width))
        rc.bind_all('<MouseWheel>', lambda e: rc.yview_scroll(-1*(e.delta//120), 'units'))
        rc.pack(side='left', fill='both', expand=True)
        rsb.pack(side='right', fill='y')

        gf.columnconfigure(0, weight=1, uniform='mc')
        gf.columnconfigure(1, weight=1, uniform='mc')

        THUMB = 190
        cells = [
            ('diffuse',   'DIFFUSE  (source)',  0, 0),
            ('height',    'HEIGHT',             0, 1),
            ('normal',    'NORMAL',             1, 0),
            ('ao',        'AO',                 1, 1),
            ('roughness', 'ROUGHNESS',          2, 0),
            ('metalness', 'METALNESS',          2, 1),
            ('edge',      'EDGE',               3, 0),
            ('emissive',  'EMISSIVE',           3, 1),
            ('rmaos',     'RMAOS  (packed)',    4, 0),
        ]
        cell_colors = {
            'diffuse': C['accent'], 'height': '#4ec9b0', 'normal': '#569cd6',
            'ao': '#9cdcfe', 'roughness': '#ce9178', 'metalness': '#dcdcaa',
            'edge': '#c586c0', 'emissive': '#ff8c00', 'rmaos': '#4ec9b0',
        }
        for map_name, disp_name, row, col in cells:
            cell = tk.Frame(gf, bg=C['panel'])
            cell.grid(row=row, column=col, padx=6, pady=6, sticky='nsew')

            tk.Label(cell, text=disp_name,
                     bg=C['panel'], fg=cell_colors.get(map_name, C['accent']),
                     font=('Segoe UI', 8, 'bold')).pack(pady=(8, 2))

            cv = tk.Canvas(cell, width=THUMB, height=THUMB,
                           bg='#111111', highlightthickness=1,
                           highlightbackground=C['border'], cursor='hand2')
            cv.pack(padx=10, pady=2)
            cv.create_text(THUMB//2, THUMB//2, text='No image',
                           fill=C['text_dim'], font=('Segoe UI', 8), tags='ph')
            cv.bind('<Button-1>',
                    lambda e, n=map_name: self._mat_open_preview(n))

            br = tk.Frame(cell, bg=C['panel'])
            br.pack(pady=(2, 8))
            sb = self._mkbtn(br, '💾  Save As…',
                              lambda n=map_name: self._mat_save_as(n),
                              bg=C['panel2'], fg=C['text_dim'], pad=(8, 3),
                              font=('Segoe UI', 8))
            sb.pack()

            sl = tk.Label(cell, text='—', bg=C['panel'], fg=C['text_dim'],
                          font=('Segoe UI', 7))
            sl.pack(pady=(0, 6))

            self._mat_cells[map_name] = {
                'canvas': cv, 'save_btn': sb, 'status': sl, 'size': THUMB}

    # ── Material helper widgets ───────────────────────────────────────────────
    def _mat_compact_slider(self, parent, label, lo, hi, res, default, on_change=None):
        """One compact label+scale+value row inside a map settings card.
        on_change(value), if given, fires (debounced upstream) on every drag tick."""
        var = tk.DoubleVar(value=default)
        row = tk.Frame(parent, bg=C['panel'])
        row.pack(fill='x', padx=8, pady=1)
        tk.Label(row, text=label, bg=C['panel'], fg=C['text'],
                 font=('Segoe UI', 7), width=10, anchor='w').pack(side='left')
        val_lbl = tk.Label(row, text=f'{default:.2f}', bg=C['panel'],
                           fg=C['text_bright'], font=('Consolas', 7), width=6)
        val_lbl.pack(side='right')
        def _on_move(v):
            val_lbl.config(text=f'{float(v):.2f}')
            if on_change:
                on_change(v)
        tk.Scale(row, from_=lo, to=hi, resolution=res, orient='horizontal',
                 variable=var, bg=C['panel'], fg=C['text'],
                 troughcolor=C['input'], highlightthickness=0,
                 showvalue=False, sliderlength=12,
                 command=_on_move
                 ).pack(fill='x', expand=True, padx=(2, 4))
        return var

    # ── Material actions ──────────────────────────────────────────────────────
    def _mat_load_preview(self):
        """Load the source image and show it in the Diffuse cell."""
        src = self._mat_src_var.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showerror('No Source', 'Please select a source texture file.')
            return
        self._mat_update_cell('diffuse', src)
        name = os.path.basename(src)
        self._mat_status.config(text=f'Loaded: {name}')
        self.root.after(0, lambda: self._mat_prog.set(0))

        # Cache a small BGR array for fast, in-memory live-preview regeneration
        # (full-res generation still only happens on "Generate All Maps").
        try:
            arr = _me._load(src) if _MAT_OK else None
            if arr is not None:
                h, w = arr.shape[:2]
                max_dim = 256
                if max(h, w) > max_dim:
                    scale = max_dim / max(h, w)
                    arr = cv2.resize(arr, (max(1, int(w*scale)), max(1, int(h*scale))),
                                     interpolation=cv2.INTER_AREA)
                self._mat_preview_src = arr
                self._mat_live_pending = set()
                self._mat_schedule_live('diffuse')
        except Exception as e:
            self._mat_preview_src = None
            print(f'[Material Preview] Could not cache live-preview source: {e}')

    # ── Live preview (in-memory, low-res, no disk I/O) ────────────────────────
    _MAT_DEPENDENTS = {
        'diffuse':   ('height', 'normal', 'ao', 'roughness', 'metalness', 'edge', 'emissive'),
        'height':    ('height', 'normal', 'ao'),
        'normal':    ('normal', 'ao'),
        'ao':        ('ao',),
        'roughness': ('roughness',),
        'metalness': ('metalness',),
        'edge':      ('edge',),
        'emissive':  ('emissive',),
    }

    def _mat_schedule_live(self, changed_map):
        """Debounced entry point -- called on every slider tick / toggle
        click. Coalesces rapid-fire calls into one regen ~100ms after the
        user stops moving, so dragging stays smooth."""
        if not _MAT_OK or not getattr(self, '_mat_live_var', None) or not self._mat_live_var.get():
            return
        if getattr(self, '_mat_preview_src', None) is None:
            return
        pending = getattr(self, '_mat_live_pending', None)
        if pending is None:
            pending = set()
            self._mat_live_pending = pending
        pending.update(self._MAT_DEPENDENTS.get(changed_map, (changed_map,)))

        if getattr(self, '_mat_live_after_id', None):
            try: self.root.after_cancel(self._mat_live_after_id)
            except Exception: pass
        self._mat_live_after_id = self.root.after(100, self._mat_regen_live)

    def _mat_regen_live(self):
        """Recompute only the maps queued in _mat_live_pending, from the
        cached low-res source, and update their thumbnails in place."""
        self._mat_live_after_id = None
        maps_to_run = getattr(self, '_mat_live_pending', set())
        self._mat_live_pending = set()
        if not maps_to_run:
            return
        base = self._mat_preview_src
        if base is None:
            return

        try:
            diffuse_kw = {k: v.get() for k, v in self._mat_svars.get('diffuse', {}).items()}
            img = _me.adjust_diffuse(base, **diffuse_kw) if diffuse_kw else base

            height_map = None
            normal_map = None
            # Compute height first if anything downstream needs it
            if any(m in ('height', 'normal', 'ao') for m in maps_to_run):
                h_kw = {k: v.get() for k, v in self._mat_svars.get('height', {}).items()}
                height_map = _me.height_from_diffuse(img, **h_kw)
            # AO's blend needs a normal map too, even if 'normal' itself isn't queued
            if 'ao' in maps_to_run:
                n_kw = {k: v.get() for k, v in self._mat_svars.get('normal', {}).items()}
                normal_map = _me.normal_from_height_and_diffuse(height_map, img, **n_kw)

            for map_name in maps_to_run:
                if not self._mat_enabled.get(map_name, tk.BooleanVar(value=True)).get():
                    continue
                kw = {k: v.get() for k, v in self._mat_svars.get(map_name, {}).items()}
                try:
                    if map_name == 'height':
                        result = height_map if height_map is not None else _me.height_from_diffuse(img, **kw)
                    elif map_name == 'normal':
                        result = _me.compute_map(map_name, img, kw, height_map=height_map)
                        normal_map = result
                    else:
                        result = _me.compute_map(map_name, img, kw, height_map=height_map, normal_map=normal_map)
                    self._mat_update_cell_from_array(map_name, result)
                except Exception as e:
                    print(f'[Material Preview] live regen failed for {map_name}: {e}')
        except Exception as e:
            print(f'[Material Preview] live regen error: {e}')

    def _mat_update_cell_from_array(self, map_name, arr):
        """Same as _mat_update_cell, but from an in-memory numpy array
        (BGR or grayscale) instead of a file path -- used for live preview
        so nothing touches disk while dragging sliders."""
        if map_name not in self._mat_cells:
            return
        info = self._mat_cells[map_name]
        cv_widget, size = info['canvas'], info['size']
        try:
            if arr.ndim == 2:
                pil = Image.fromarray(arr)
            elif arr.shape[2] == 3:
                pil = Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))
            else:
                pil = Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGRA2RGBA))
            pil.thumbnail((size, size), Image.LANCZOS)
            photo = ImageTk.PhotoImage(pil)
            self._mat_photos[map_name] = photo  # prevent GC
            cv_widget.delete('all')
            cv_widget.create_image(size // 2, size // 2, anchor='center', image=photo)
            info['status'].config(text='(live preview)', fg=C['text_dim'])
        except Exception as e:
            print(f'[Material Preview] could not display live {map_name}: {e}')

    def _mat_update_cell(self, map_name, img_path):
        """Load image, thumbnail it, display in the named preview cell."""
        if map_name not in self._mat_cells:
            return
        info = self._mat_cells[map_name]
        cv   = info['canvas']
        size = info['size']
        try:
            img = Image.open(img_path)
            img.thumbnail((size, size), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._mat_photos[map_name] = photo   # prevent GC
            self._mat_paths[map_name]  = img_path
            cv.delete('all')
            cv.create_image(size // 2, size // 2, anchor='center', image=photo)
            info['status'].config(text=os.path.basename(img_path),
                                  fg=C['success'])
        except Exception as e:
            cv.delete('all')
            cv.create_text(size // 2, size // 2, text=f'Error:\n{e}',
                           fill=C['error'], font=('Segoe UI', 7),
                           width=size - 10, justify='center')

    def _mat_open_preview(self, map_name):
        """Open a full-size preview window for a generated map."""
        path = self._mat_paths.get(map_name)
        if path and os.path.exists(path):
            self._preview_image(path)
        else:
            self._mat_status.config(text=f'No image for {map_name} yet.')

    def _preview_image(self, path):
        """Open a standalone preview window for any image path."""
        try:
            img = Image.open(path)
            thumb = img.copy()
            thumb.thumbnail((640, 640), Image.LANCZOS)
            photo = ImageTk.PhotoImage(thumb)
            win = tk.Toplevel(self.root)
            win.title(f'Preview — {os.path.basename(path)}')
            win.configure(bg=C['bg'])
            win.resizable(False, False)
            tk.Label(win, image=photo, bg=C['bg']).pack(padx=10, pady=10)
            tk.Label(win,
                     text=f'{os.path.basename(path)}  |  {img.width}×{img.height}  |  {img.mode}',
                     bg=C['bg'], fg=C['text_dim'], font=('Segoe UI', 8)
                     ).pack(pady=(0, 10))
            win._photo = photo
        except Exception as e:
            messagebox.showerror('Preview Error', str(e))

    def _mat_save_as(self, map_name):
        """Save a generated map to a user-chosen path."""
        src = self._mat_paths.get(map_name)
        if not src or not os.path.exists(src):
            messagebox.showinfo('Not generated',
                                f'{map_name} has not been generated yet.')
            return
        ext = os.path.splitext(src)[1]
        dst = filedialog.asksaveasfilename(
            title=f'Save {map_name} map',
            initialfile=os.path.basename(src),
            defaultextension=ext,
            filetypes=[('PNG', '*.png'), ('TGA', '*.tga'),
                       ('BMP', '*.bmp'), ('All', '*.*')])
        if dst:
            import shutil
            shutil.copy2(src, dst)
            self._mat_status.config(text=f'Saved: {os.path.basename(dst)}')

    def _open_material_preview(self):
        """Open (or focus) the 3D material preview window."""
        global _preview_win
        if not _MP_OK:
            messagebox.showerror('Missing Module',
                'material_preview.py not found alongside the executable.')
            return
        if _preview_win is None or not (_preview_win.win and
                                         _preview_win.win.winfo_exists()):
            _preview_win = _mp.MaterialPreviewWindow(self)
        _preview_win.open()

    def _mat_pack_rmaos(self):
        """Pack generated maps into RMAOS right now using current channel settings."""
        if not _MAT_OK:
            messagebox.showerror('Missing Module', 'material_engine.py not found.')
            return
        out = self._mat_out_var.get().strip()
        if not out:
            messagebox.showerror('No Output', 'Please choose an output folder first.')
            return

        # Load each generated map from its saved path
        rmaos_maps = {}
        for mn in ('roughness', 'metalness', 'ao', 'height', 'edge', 'emissive'):
            path = self._mat_paths.get(mn)
            if path and os.path.exists(path):
                import cv2 as _cv2
                loaded = _cv2.imread(path, -1)
                if loaded is not None:
                    rmaos_maps[mn] = loaded

        if not rmaos_maps:
            messagebox.showwarning('No Maps',
                'No generated maps found yet.\n'
                'Run Generate All Maps first, or load individual maps.')
            return

        # Read channel assignments from the comboboxes
        svars = self._mat_svars.get('rmaos', {})
        r_src = svars.get('r_src', tk.StringVar(value='roughness')).get()
        g_src = svars.get('g_src', tk.StringVar(value='metalness')).get()
        b_src = svars.get('b_src', tk.StringVar(value='ao')).get()
        a_src = svars.get('a_src', tk.StringVar(value='smoothness')).get()

        def run():
            self._mat_status.config(
                text=f'Packing RMAOS  R={r_src}  G={g_src}  B={b_src}  A={a_src}…',
                fg=C['text_dim'])
            self._mat_prog.set(10)
            try:
                rmaos_arr = _me.rmaos_from_maps(
                    rmaos_maps, r_src=r_src, g_src=g_src, b_src=b_src, a_src=a_src)

                src_stem = Path(self._mat_src_var.get()).stem if self._mat_src_var.get() else 'texture'
                fmt = self._mat_fmt_var.get().lower()
                out_path = str(Path(out) / f'{src_stem}_rmaos.{fmt}')
                from PIL import Image as _PILImg
                _PILImg.fromarray(rmaos_arr).save(out_path)

                self.root.after(0, lambda: self._mat_update_cell('rmaos', out_path))
                self.root.after(0, lambda: self._mat_prog.set(100))
                self.root.after(0, lambda: self._mat_status.config(
                    text=f'✓ RMAOS packed → {Path(out_path).name}',
                    fg=C['success']))
            except Exception as e:
                self.root.after(0, lambda: self._mat_status.config(
                    text=f'✗ RMAOS pack failed: {e}', fg=C['error']))
                self.root.after(0, lambda: self._mat_prog.set(0))

        threading.Thread(target=run, daemon=True).start()

    def _mat_generate(self):
        """Build settings dict and run generate_all_maps in a background thread."""
        if not _MAT_OK:
            messagebox.showerror('Missing Module',
                                 'material_engine.py not found alongside the exe.')
            return

        src = self._mat_src_var.get().strip()
        out = self._mat_out_var.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showerror('No Source', 'Please select a source texture.')
            return
        if not out:
            messagebox.showerror('No Output', 'Please choose an output folder.')
            return

        # Build settings dict from current UI vars
        settings = {'enabled': {}, 'diffuse': {}, 'height': {}, 'normal': {}, 'ao': {},
                    'roughness': {}, 'metalness': {}, 'edge': {}, 'emissive': {},
                    'rmaos': {}}

        settings['diffuse'] = {k: v.get() for k, v in self._mat_svars.get('diffuse', {}).items()}

        for map_name in _me.MAP_ORDER:
            settings['enabled'][map_name] = self._mat_enabled.get(map_name,
                                            tk.BooleanVar(value=True)).get()
            svars = self._mat_svars.get(map_name, {})
            for key, var in svars.items():
                if key.startswith('_'): continue
                settings[map_name][key] = var.get()

        fmt = self._mat_fmt_var.get().lower()

        # Clear status
        for name, info in self._mat_cells.items():
            if name != 'diffuse':
                info['status'].config(text='—', fg=C['text_dim'])

        def _log(msg, c=None):
            fg = {'success': C['success'], 'warn': C['warn'],
                  'error': C['error']}.get(c, C['text_dim'])
            self.root.after(0, lambda m=msg, f=fg:
                            self._mat_status.config(text=m, fg=f))

        def _prog(done, total):
            pct = done / total * 100 if total else 0
            self.root.after(0, lambda p=pct: self._mat_prog.set(p))

        def run():
            self.root.after(0, lambda: self._mat_prog.set(0))
            results = _me.generate_all_maps(
                src, settings, out, fmt=fmt,
                log=_log, progress=_prog, cancelled=None)

            # Update each preview cell with its generated map
            for map_name, path in results.items():
                self.root.after(0,
                    lambda n=map_name, p=path: self._mat_update_cell(n, p))

            self.root.after(0, lambda: self._mat_prog.set(100))
            self.root.after(0, lambda n=len(results):
                self._mat_status.config(
                    text=f'✓ Done — {n} map(s) saved to {out}',
                    fg=C['success']))

        threading.Thread(target=run, daemon=True).start()

    # DUAL LAYER MATERIAL BUILDER
    # =========================================================================
    def _build_dual_layer_tab(self, parent):
        """8-map dual-layer material generator from a single diffuse image."""
        self._dl_src    = None
        self._dl_maps   = {}
        self._dl_photos = {}

        left = tk.Frame(parent, bg=C['surface'], width=320)
        left.pack(side='left', fill='y')
        left.pack_propagate(False)
        right = tk.Frame(parent, bg=C['bg'])
        right.pack(side='right', fill='both', expand=True)

        # ── Left controls ────────────────────────────────────────────────────
        _, sf = self._make_scrollable(left, C['surface'])

        self._section_lbl(sf, 'DUAL LAYER MATERIAL BUILDER',
            'Generate 8 PBR maps (BaseColor, Normal, Roughness, Metallic, Height, '
            'CoatColor, CoatNormal, CoatRoughness) from a single diffuse image.')

        # Source picker
        src_row = tk.Frame(sf, bg=C['surface'])
        src_row.pack(fill='x', padx=16, pady=4)
        self._dl_src_lbl = tk.Label(src_row, text='No image loaded',
                                     bg=C['surface'], fg=C['text_dim'],
                                     font=('Segoe UI', 8), wraplength=240, anchor='w')
        self._dl_src_lbl.pack(side='left', fill='x', expand=True)

        def _pick_src():
            f = filedialog.askopenfilename(
                title='Select diffuse image',
                filetypes=[('Images','*.png *.jpg *.jpeg *.bmp *.tga *.dds'),
                           ('All','*.*')])
            if not f: return
            try:
                self._dl_src = Image.open(f).convert('RGB')
                self._dl_src_lbl.config(text=os.path.basename(f), fg=C['text'])
                self._dl_btn_gen.config(state='normal')
                self._dl_update_cell('basecolor', self._dl_src)
            except Exception as e:
                messagebox.showerror('Load Error', str(e))

        self._mkbtn(sf, '📁  Load Diffuse Image', _pick_src,
                    pad=(16,8), font=('Segoe UI', 9, 'bold')
                    ).pack(fill='x', padx=16, pady=(2,8))

        self._sep(sf)
        self._section_lbl(sf, 'MATERIAL SETTINGS')

        # Sliders
        self._dl_vars = {}
        def _sl(lbl, lo, hi, default, key):
            var = self._mat_compact_slider(sf, lbl, lo, hi,
                                           round((hi-lo)/100, 4), default)
            self._dl_vars[key] = var
            return var

        _sl('Normal Strength',   0.1, 10.0, 3.0,  'normal_strength')
        _sl('Roughness Contrast',0.5,  3.0, 1.0,  'rough_contrast')

        self._dl_rough_inv = tk.BooleanVar(value=True)
        ttk.Checkbutton(sf, text='Invert Roughness',
                        variable=self._dl_rough_inv,
                        style='Dark.TCheckbutton').pack(anchor='w', padx=16, pady=2)

        _sl('Metallic Level',    0.0,  1.0, 0.0,  'metallic')
        _sl('Parallax Scale',    0.0,  0.1, 0.05, 'parallax')

        self._dl_height_inv = tk.BooleanVar(value=False)
        ttk.Checkbutton(sf, text='Invert Height',
                        variable=self._dl_height_inv,
                        style='Dark.TCheckbutton').pack(anchor='w', padx=16, pady=2)

        self._sep(sf)
        self._section_lbl(sf, 'CLEAR COAT')

        _sl('Coat Opacity',   0.0, 1.0, 0.0,  'coat_opacity')
        _sl('Coat Roughness', 0.0, 1.0, 0.2,  'coat_roughness')

        self._dl_coat_color = '#ffffff'
        cc_row = tk.Frame(sf, bg=C['surface'])
        cc_row.pack(fill='x', padx=16, pady=4)
        tk.Label(cc_row, text='Coat Color', bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 8), width=14, anchor='w').pack(side='left')
        self._dl_cc_btn = tk.Button(cc_row, text='  ■  #ffffff  ',
                                     bg='#ffffff', fg='#111',
                                     relief='flat', cursor='hand2',
                                     font=('Segoe UI', 8),
                                     command=self._dl_pick_coat_color)
        self._dl_cc_btn.pack(side='left', padx=4)

        self._dl_use_coat_normal = tk.BooleanVar(value=False)
        ttk.Checkbutton(sf, text='Use blurred normal as coat normal',
                        variable=self._dl_use_coat_normal,
                        style='Dark.TCheckbutton').pack(anchor='w', padx=16, pady=4)

        self._sep(sf)

        self._dl_btn_gen = self._mkbtn(
            sf, '⚡  Generate All 8 Maps', self._dl_generate,
            pad=(16,12), font=('Segoe UI', 11, 'bold'))
        self._dl_btn_gen.pack(fill='x', padx=16, pady=4)
        self._dl_btn_gen.config(state='disabled')

        self._dl_prog = tk.DoubleVar(value=0)
        ttk.Progressbar(sf, variable=self._dl_prog, maximum=100
                        ).pack(fill='x', padx=16, pady=(0,4))

        self._mkbtn(sf, '💾  Save All Maps', self._dl_save_all,
                    bg=C['panel'], fg=C['text'], pad=(16,8),
                    font=('Segoe UI', 9)).pack(fill='x', padx=16, pady=(0,14))

        # ── Right: 4×2 preview grid ───────────────────────────────────────────
        rc = tk.Canvas(right, bg=C['bg'], highlightthickness=0)
        rsb = ttk.Scrollbar(right, orient='vertical', command=rc.yview)
        gf = tk.Frame(rc, bg=C['bg'])
        gf.bind('<Configure>', lambda e: rc.configure(scrollregion=rc.bbox('all')))
        gw = rc.create_window((0,0), window=gf, anchor='nw')
        rc.configure(yscrollcommand=rsb.set)
        rc.bind('<Configure>', lambda e: rc.itemconfig(gw, width=e.width))
        rc.pack(side='left', fill='both', expand=True)
        rsb.pack(side='right', fill='y')

        for c in range(4):
            gf.columnconfigure(c, weight=1, uniform='dlc')

        THUMB = 170
        CELLS = [
            ('basecolor',     'BASE COLOR',    C['accent'],   0, 0),
            ('normal',        'NORMAL',        '#569cd6',     0, 1),
            ('roughness',     'ROUGHNESS',     '#ce9178',     0, 2),
            ('metallic',      'METALLIC',      '#dcdcaa',     0, 3),
            ('height',        'HEIGHT',        '#4ec9b0',     1, 0),
            ('coatcolor',     'COAT COLOR',    '#c586c0',     1, 1),
            ('coatnormal',    'COAT NORMAL',   '#9cdcfe',     1, 2),
            ('coatroughness', 'COAT ROUGHNESS','#ff8c00',     1, 3),
        ]
        self._dl_cells = {}
        for key, disp, col, row, colnum in CELLS:
            cell = tk.Frame(gf, bg=C['panel'])
            cell.grid(row=row, column=colnum, padx=5, pady=5, sticky='nsew')
            tk.Label(cell, text=disp, bg=C['panel'], fg=col,
                     font=('Segoe UI', 8, 'bold')).pack(pady=(6,2))
            cv = tk.Canvas(cell, width=THUMB, height=THUMB, bg='#111',
                           highlightthickness=1, highlightbackground=C['border'],
                           cursor='hand2')
            cv.pack(padx=6, pady=2)
            cv.create_text(THUMB//2, THUMB//2, text='No image',
                           fill=C['text_dim'], font=('Segoe UI', 7), tags='ph')
            cv.bind('<Button-1>', lambda e, k=key: self._dl_open_preview(k))
            tk.Label(cell, text='—', bg=C['panel'], fg=C['text_dim'],
                     font=('Segoe UI', 7)).pack(pady=(0,6))
            self._dl_cells[key] = {'canvas': cv, 'size': THUMB}

    def _dl_pick_coat_color(self):
        from tkinter import colorchooser
        c = colorchooser.askcolor(title='Coat Color',
                                   color=self._dl_coat_color)
        if c and c[1]:
            self._dl_coat_color = c[1]
            r, g, b = [int(c[1][i:i+2],16) for i in (1,3,5)]
            lum = r*0.299 + g*0.587 + b*0.114
            self._dl_cc_btn.config(bg=c[1], text=f'  ■  {c[1]}  ',
                                    fg='white' if lum < 128 else '#111')

    def _dl_update_cell(self, key, img):
        if key not in self._dl_cells: return
        info = self._dl_cells[key]
        sz   = info['size']
        thumb = img.copy()
        thumb.thumbnail((sz, sz), Image.LANCZOS)
        photo = ImageTk.PhotoImage(thumb)
        self._dl_photos[key] = photo
        cv = info['canvas']
        cv.delete('all')
        cv.create_image(sz//2, sz//2, anchor='center', image=photo)

    def _dl_open_preview(self, key):
        img = self._dl_maps.get(key)
        if img:
            self._preview_image_pil(img, key)

    def _preview_image_pil(self, img, title=''):
        thumb = img.copy(); thumb.thumbnail((640,640), Image.LANCZOS)
        photo = ImageTk.PhotoImage(thumb)
        win = tk.Toplevel(self.root)
        win.title(f'Preview — {title}')
        win.configure(bg=C['bg']); win.resizable(False, False)
        tk.Label(win, image=photo, bg=C['bg']).pack(padx=10, pady=10)
        tk.Label(win, text=f'{img.width}×{img.height}  |  {img.mode}',
                 bg=C['bg'], fg=C['text_dim'], font=('Segoe UI', 8)
                 ).pack(pady=(0,10))
        win._photo = photo

    def _dl_generate(self):
        if self._dl_src is None: return

        def run():
            self._dl_prog.set(0)
            img  = self._dl_src
            w, h = img.size
            arr  = np.array(img).astype(np.float32) / 255.0
            lum  = (0.299*arr[:,:,0] + 0.587*arr[:,:,1] + 0.114*arr[:,:,2])

            steps = 8
            def prog(i): self.root.after(0, lambda: self._dl_prog.set(i/steps*100))

            # 1 BaseColor
            self._dl_maps['basecolor'] = img.copy()
            self.root.after(0, lambda: self._dl_update_cell('basecolor', img))
            prog(1)

            # 2 Normal — Sobel via cv2
            import cv2 as _cv2
            strength = self._dl_vars['normal_strength'].get()
            dx = _cv2.Sobel(lum, _cv2.CV_32F, 1, 0, ksize=3) * strength
            dy = _cv2.Sobel(lum, _cv2.CV_32F, 0, 1, ksize=3) * strength
            dz = np.ones_like(lum)
            length = np.sqrt(dx*dx + dy*dy + dz*dz)
            nx = np.clip((dx/length*0.5+0.5)*255, 0, 255)
            ny = np.clip((dy/length*0.5+0.5)*255, 0, 255)
            nz = np.clip((dz/length*0.5+0.5)*255, 0, 255)
            # Store as RGB (R=X, G=Y, B=Z)
            norm_arr = np.stack([nx, ny, nz], axis=-1).astype(np.uint8)
            norm_img = Image.fromarray(norm_arr)
            self._dl_maps['normal'] = norm_img
            self.root.after(0, lambda: self._dl_update_cell('normal', norm_img))
            prog(2)

            # 3 Roughness
            rough = lum.copy()
            if self._dl_rough_inv.get(): rough = 1.0 - rough
            contrast = self._dl_vars['rough_contrast'].get()
            rough = np.clip((rough - 0.5)*contrast + 0.5, 0, 1)
            rough_img = Image.fromarray((rough*255).astype(np.uint8))
            self._dl_maps['roughness'] = rough_img
            self.root.after(0, lambda: self._dl_update_cell('roughness', rough_img))
            prog(3)

            # 4 Metallic (flat)
            mv = int(self._dl_vars['metallic'].get() * 255)
            metal_img = Image.new('L', (w, h), mv)
            self._dl_maps['metallic'] = metal_img
            self.root.after(0, lambda: self._dl_update_cell('metallic', metal_img))
            prog(4)

            # 5 Height/Parallax
            ht = lum.copy()
            if self._dl_height_inv.get(): ht = 1.0 - ht
            height_img = Image.fromarray((ht*255).astype(np.uint8))
            self._dl_maps['height'] = height_img
            self.root.after(0, lambda: self._dl_update_cell('height', height_img))
            prog(5)

            # 6 Coat Color
            hex_c = self._dl_coat_color
            rgb = tuple(int(hex_c[i:i+2],16) for i in (1,3,5))
            op  = self._dl_vars['coat_opacity'].get()
            coat = Image.new('RGB', (w, h), rgb)
            if op < 1.0:
                coat = Image.blend(Image.new('RGB',(w,h),(0,0,0)), coat, op)
            self._dl_maps['coatcolor'] = coat
            self.root.after(0, lambda: self._dl_update_cell('coatcolor', coat))
            prog(6)

            # 7 Coat Normal
            if self._dl_use_coat_normal.get():
                from PIL import ImageFilter
                cn = norm_img.filter(ImageFilter.GaussianBlur(5))
            else:
                cn = Image.new('RGB', (w, h), (128, 128, 255))
            self._dl_maps['coatnormal'] = cn
            self.root.after(0, lambda: self._dl_update_cell('coatnormal', cn))
            prog(7)

            # 8 Coat Roughness (flat)
            cr = int(self._dl_vars['coat_roughness'].get() * 255)
            cr_img = Image.new('L', (w, h), cr)
            self._dl_maps['coatroughness'] = cr_img
            self.root.after(0, lambda: self._dl_update_cell('coatroughness', cr_img))
            prog(8)

            self.root.after(0, lambda: self._dl_prog.set(100))

        threading.Thread(target=run, daemon=True).start()

    def _dl_save_all(self):
        if not self._dl_maps:
            messagebox.showwarning('No Maps', 'Generate maps first.')
            return
        folder = filedialog.askdirectory(title='Select output folder',
                                          initialdir=self.output_dir.get())
        if not folder: return
        names = {
            'basecolor':     'BaseColor.png',
            'normal':        'Normal.png',
            'roughness':     'Roughness.png',
            'metallic':      'Metallic.png',
            'height':        'Height_Parallax.png',
            'coatcolor':     'CoatColor.png',
            'coatnormal':    'CoatNormal.png',
            'coatroughness': 'CoatRoughness.png',
        }
        saved = 0
        for key, fname in names.items():
            if key in self._dl_maps:
                self._dl_maps[key].save(os.path.join(folder, fname))
                saved += 1
        messagebox.showinfo('Saved', f'Saved {saved} maps to:\n{folder}')


    # =========================================================================
    # _p MAP BUILDER  (batch Normal + Height from diffuse PNGs)
    # =========================================================================
    def _build_pmap_tab(self, parent):
        """Batch-generate _n (normal) and _p (height) maps from a folder of PNG diffuse textures."""
        left = tk.Frame(parent, bg=C['surface'], width=420)
        left.pack(side='left', fill='y')
        left.pack_propagate(False)
        right = tk.Frame(parent, bg=C['panel'])
        right.pack(side='right', fill='both', expand=True)

        _, sf = self._make_scrollable(left, C['surface'])

        self._section_lbl(sf, '_p MAP BUILDER',
            'Batch-generates _n (normal map) and _p (height map) PNG files for every '
            'diffuse PNG in the input folder. Skips files that already have known '
            'map suffixes.')

        # Folders
        self._pm_in_var  = self.input_dir
        self._pm_out_var = self.output_dir
        self._folder_row(sf, 'Input Folder',  self._pm_in_var)
        self._folder_row(sf, 'Output Folder', self._pm_out_var)

        self._sep(sf)
        self._section_lbl(sf, 'SETTINGS')

        self._pm_vars = {}
        def _sl(lbl, lo, hi, res, default, key):
            row = tk.Frame(sf, bg=C['surface']); row.pack(fill='x', padx=16, pady=3)
            tk.Label(row, text=lbl, bg=C['surface'], fg=C['text'],
                     font=('Segoe UI', 8), width=18, anchor='w').pack(side='left')
            var = tk.DoubleVar(value=default)
            val_lbl = tk.Label(row, text=f'{default:.3f}', bg=C['surface'],
                               fg=C['text_bright'], font=('Consolas', 8), width=6)
            val_lbl.pack(side='right')
            tk.Scale(row, from_=lo, to=hi, resolution=res, orient='horizontal',
                     variable=var, bg=C['surface'], fg=C['text'],
                     troughcolor=C['input'], highlightthickness=0,
                     showvalue=False, sliderlength=14,
                     command=lambda v, l=val_lbl: l.config(text=f'{float(v):.3f}')
                     ).pack(fill='x', expand=True, padx=(0,4))
            self._pm_vars[key] = var

        _sl('Normal Strength',   0.5, 20.0, 0.1,  6.0,  'strength')
        _sl('Blur Radius',       0.0,  8.0, 0.25, 2.5,  'blur_radius')
        _sl('Gradient Mult.',    0.05, 1.0, 0.05, 0.25, 'grad_mult')

        self._pm_norm_height = tk.BooleanVar(value=False)
        ttk.Checkbutton(sf, text='Normalize Height (stretch to full 0-255 range)',
                        variable=self._pm_norm_height,
                        style='Dark.TCheckbutton').pack(anchor='w', padx=16, pady=4)

        self._sep(sf)
        tk.Label(sf, text='Skip files ending with (comma-separated):',
                 bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8)).pack(anchor='w', padx=16)
        self._pm_skip_var = tk.StringVar(value='_n, _g, _m, _p, _r, _ao')
        tk.Entry(sf, textvariable=self._pm_skip_var, bg=C['input'],
                 fg=C['text'], insertbackground=C['text'],
                 relief='flat', font=('Segoe UI', 8)
                 ).pack(fill='x', padx=16, pady=4)

        tk.Label(sf, text='Output format:',
                 bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8)).pack(anchor='w', padx=16)
        self._pm_fmt_var = tk.StringVar(value='PNG')
        fmt_row = tk.Frame(sf, bg=C['surface']); fmt_row.pack(fill='x', padx=16, pady=4)
        for fmt in ['PNG','TGA','BMP']:
            ttk.Radiobutton(fmt_row, text=fmt, variable=self._pm_fmt_var, value=fmt,
                            style='Dark.TRadiobutton').pack(side='left', padx=(0,12))

        self._sep(sf)
        self._mkbtn(sf, '▶  Run Batch Builder', self._pm_run,
                    pad=(16,12), font=('Segoe UI', 10, 'bold')
                    ).pack(fill='x', padx=16, pady=6)

        self._pm_prog = tk.DoubleVar(value=0)
        ttk.Progressbar(sf, variable=self._pm_prog, maximum=100
                        ).pack(fill='x', padx=16, pady=(0,4))
        tk.Frame(sf, bg=C['surface'], height=8).pack()

        # Right: log
        tk.Label(right, text='PROCESSING LOG', bg=C['panel'], fg=C['text_dim'],
                 font=('Consolas', 8, 'bold')).pack(anchor='w', padx=10, pady=(10,2))
        self.pm_log = self._console(right)
        self.pm_log.pack(fill='both', expand=True, padx=8, pady=(0,4))
        self._mkbtn(right, 'Clear', lambda: self._clear_log(self.pm_log),
                    bg=C['panel2'], fg=C['text_dim'], pad=(8,3),
                    font=('Segoe UI', 8)).pack(anchor='e', padx=8, pady=(0,8))

    def _pm_run(self):
        in_dir  = self._pm_in_var.get().strip()
        out_dir = self._pm_out_var.get().strip()
        if not in_dir or not os.path.isdir(in_dir):
            messagebox.showerror('Invalid Input', 'Please select a valid input folder.')
            return
        if not out_dir:
            messagebox.showerror('Invalid Output', 'Please select an output folder.')
            return

        strength   = self._pm_vars['strength'].get()
        blur_r     = self._pm_vars['blur_radius'].get()
        grad_mult  = self._pm_vars['grad_mult'].get()
        norm_h     = self._pm_norm_height.get()
        skip_raw   = self._pm_skip_var.get()
        skip_sfx   = tuple(s.strip().lower() for s in skip_raw.split(',') if s.strip())
        fmt        = self._pm_fmt_var.get().lower()

        def run():
            self._clear_log(self.pm_log)
            self._pm_prog.set(0)
            self._log(self.pm_log,
                      f'Input:  {in_dir}\nOutput: {out_dir}\n'
                      f'Strength={strength}  Blur={blur_r}  GradMult={grad_mult}\n'
                      f'Skip: {skip_sfx}\n')

            import cv2 as _cv2
            from PIL import ImageFilter as _IF

            # Collect all PNGs
            all_files = []
            for root_d, _, files in os.walk(in_dir):
                for f in files:
                    if not f.lower().endswith('.png'):
                        continue
                    stem = os.path.splitext(f)[0].lower()
                    if any(stem.endswith(s) for s in skip_sfx):
                        continue
                    all_files.append(os.path.join(root_d, f))

            total = len(all_files)
            if not total:
                self._log(self.pm_log, 'No eligible PNG files found.', C['warn'])
                return
            self._log(self.pm_log, f'Found {total} file(s) to process.\n')

            ok_count = 0
            for done, fpath in enumerate(all_files, 1):
                rel   = os.path.relpath(fpath, in_dir)
                rel_d = os.path.dirname(rel)
                base  = os.path.splitext(os.path.basename(fpath))[0]
                out_d = os.path.join(out_dir, rel_d)
                os.makedirs(out_d, exist_ok=True)

                self._log(self.pm_log, f'Processing: {rel}')
                try:
                    img = Image.open(fpath).convert('RGB')

                    # Height field
                    gray = img.convert('L')
                    if blur_r > 0:
                        gray = gray.filter(_IF.GaussianBlur(radius=blur_r))
                    height = np.array(gray).astype(np.float32) / 255.0
                    if norm_h:
                        lo_, hi_ = height.min(), height.max()
                        if hi_ - lo_ > 0:
                            height = (height - lo_) / (hi_ - lo_)

                    # Normal map via Sobel
                    dx = _cv2.Sobel(height, _cv2.CV_32F, 1, 0, ksize=3) * grad_mult
                    dy = _cv2.Sobel(height, _cv2.CV_32F, 0, 1, ksize=3) * grad_mult
                    dz = np.ones_like(height) / strength
                    nm = np.stack([dx, dy, dz], axis=2)
                    nlen = np.linalg.norm(nm, axis=2, keepdims=True)
                    nm = nm / np.clip(nlen, 1e-8, None)
                    nm_u8 = ((nm + 1.0) * 0.5 * 255.0).clip(0,255).astype(np.uint8)
                    normal_img = Image.fromarray(nm_u8)

                    # Height map
                    height_img = Image.fromarray(
                        (height * 255.0).clip(0,255).astype(np.uint8))

                    normal_img.save(os.path.join(out_d, f'{base}_n.{fmt}'))
                    height_img.save(os.path.join(out_d, f'{base}_p.{fmt}'))

                    self._log(self.pm_log,
                              f'  ✓ {base}_n.{fmt}  +  {base}_p.{fmt}', C['success'])
                    ok_count += 1
                except Exception as e:
                    self._log(self.pm_log, f'  ✗ {e}', C['error'])

                self.root.after(0, lambda p=done/total*100: self._pm_prog.set(p))

            self._pm_prog.set(100)
            self._log(self.pm_log,
                      f'\n✓ Done — {ok_count}/{total} processed.', C['success'])

        threading.Thread(target=run, daemon=True).start()

# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    root = tk.Tk()
    try:
        icon = resource_path('icon.ico')
        if os.path.exists(icon): root.iconbitmap(icon)
    except Exception: pass
    TextureGeneratorApp(root)
    root.mainloop()

if __name__ == '__main__':
    main()


    # =========================================================================


    # =========================================================================
