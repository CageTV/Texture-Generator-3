"""
Texture Generator v1.2
Self-contained portable Windows GUI.
No ImageMagick required — format conversions via PIL + texconv.
texconv.exe is bundled inside this executable.
"""

import os, sys, subprocess, shutil, threading, tempfile, json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from PIL import Image, ImageTk
import numpy as np
from ui_widgets import RoundedButton
try:
    from version import VERSION
except Exception:
    VERSION = "1.1.6"

try:
    import pbr_engine as _pe
    _PBR_OK = True
    _PBR_ERR = None
except Exception as e:
    _PBR_OK = False
    _PBR_ERR = f'{type(e).__name__}: {e}'

try:
    import material_engine as _me
    _MAT_OK = True
    _MAT_ERR = None
except Exception as e:
    _MAT_OK = False
    _MAT_ERR = f'{type(e).__name__}: {e}'

# LAZY AI - don't import at startup, fixes slow load
_ai = None
_AI_OK = False
_AI_ERR = None
def _lazy_load_ai():
    global _ai, _AI_OK, _AI_ERR
    if _ai is not None:
        return _ai
    try:
        import ai_depth_engine as _ai_mod
        _ai = _ai_mod
        _AI_OK = _ai_mod.is_ai_available()
        _AI_ERR = None
        return _ai
    except Exception as e:
        _AI_OK = False
        _AI_ERR = f'{type(e).__name__}: {e}'
        return None


try:
    import material_preview as _mp
    _MP_OK = True
    _MP_ERR = None
except Exception as e:
    _MP_OK = False
    _MP_ERR = f'{type(e).__name__}: {e}'

try:
    import gpu_engine as _ge
    _GPU_OK = _ge.gpu_available()   # probes for a real GL context, not just the import
except Exception:
    _GPU_OK = False

_preview_win = None  # singleton


class _Tooltip:
    """Minimal hover tooltip. The quick-launch icon buttons are small by
    design (room for more apps later without crowding), so they need a
    label-on-hover to stay identifiable rather than a text label baked in."""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind('<Enter>', self._show, add='+')
        widget.bind('<Leave>', self._hide, add='+')

    def _show(self, _e=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 6
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f'+{x}+{y}')
        tk.Label(self.tip, text=self.text, bg='#2b2b2b', fg=C['text'],
                 font=('Segoe UI', 8), padx=6, pady=3, relief='solid', bd=1
                 ).pack()

    def _hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class _CustomToolDialog(tk.Toplevel):
    """Modal for setting up a custom quick-launch slot: a Name field and a
    Path field (with its own Browse button) both visible in the same
    window at once - not two separate sequential popups, since a name
    prompt closing on its own made it look like nothing else was going to
    happen, and the file-browse dialog that followed was easy to miss.

    .result is (name_or_None, path) on OK, or None if cancelled.
    """
    def __init__(self, parent, initial_name='', initial_path=''):
        super().__init__(parent)
        self.result = None
        self.title('Custom Shortcut')
        self.configure(bg=C['bg'])
        self.resizable(False, False)
        self.transient(parent)

        tk.Label(self, text='Name (shown on hover):', bg=C['bg'], fg=C['text'],
                 font=('Segoe UI', 9)).pack(anchor='w', padx=10, pady=(12, 0))
        self.name_var = tk.StringVar(value=initial_name)
        tk.Entry(self, textvariable=self.name_var, bg=C['input'], fg=C['text'],
                 insertbackground=C['text'], relief='flat', font=('Segoe UI', 9), bd=2,
                 width=44).pack(fill='x', padx=10, pady=(3, 0))

        tk.Label(self, text='Path to .exe:', bg=C['bg'], fg=C['text'],
                 font=('Segoe UI', 9)).pack(anchor='w', padx=10, pady=(10, 0))
        row = tk.Frame(self, bg=C['bg'])
        row.pack(fill='x', padx=10, pady=(3, 0))
        self.path_var = tk.StringVar(value=initial_path)
        tk.Entry(row, textvariable=self.path_var, bg=C['input'], fg=C['text'],
                 insertbackground=C['text'], relief='flat', font=('Segoe UI', 9), bd=2
                 ).pack(side='left', fill='x', expand=True)
        RoundedButton(row, text='Browse…', command=self._browse,
                      bg=C['accent'], fg='white', font=('Segoe UI', 9, 'bold'),
                      pad=(10, 4)).pack(side='left', padx=(6, 0))

        btns = tk.Frame(self, bg=C['bg'])
        btns.pack(fill='x', padx=10, pady=12)
        RoundedButton(btns, text='OK', command=self._ok,
                      bg=C['success'], fg='white', font=('Segoe UI', 9, 'bold'),
                      pad=(16, 5)).pack(side='right')
        RoundedButton(btns, text='Cancel', command=self.destroy,
                      bg=C['panel2'], fg=C['text'], font=('Segoe UI', 9, 'bold'),
                      pad=(16, 5)).pack(side='right', padx=(0, 6))

        self.update_idletasks()
        self.grab_set()
        self.wait_window(self)

    def _browse(self):
        p = filedialog.askopenfilename(
            title='Locate the .exe', parent=self,
            filetypes=[('Executable', '*.exe'), ('All files', '*.*')])
        if p:
            self.path_var.set(p)

    def _ok(self):
        name = self.name_var.get().strip()
        path = self.path_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror('Custom Shortcut', 'Please Browse to a valid .exe first.', parent=self)
            return
        self.result = (name or None, path)
        self.destroy()


# ─── Paths ────────────────────────────────────────────────────────────────────
def resource_path(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

def texconv_exe():
    """texconv is bundled inside the exe (_MEIPASS) or next to the script."""
    return resource_path('texconv.exe')

def bsarch_exe():
    """BSArch.exe — bundled inside the exe or next to the script."""
    return resource_path('BSArch.exe')

def powershell_exe():
    """Bundled portable powershell.exe for running the PBR JSON Builder's
    PowerShell scripts (Step1.ps1 / Step2.ps1) without depending on the
    system's own PowerShell install/execution policy."""
    return resource_path('powershell.exe')

def app_config_dir():
    """Where user-editable config (external_tools.json, etc.) lives - next
    to the exe when frozen, next to this script otherwise. Deliberately
    NOT resource_path()'s _MEIPASS: that folder is read-only and gets
    wiped between runs, so anything we need to persist has to live
    somewhere else."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

EXTERNAL_TOOLS_FILE = os.path.join(app_config_dir(), 'external_tools.json')

def load_external_tool_paths():
    try:
        with open(EXTERNAL_TOOLS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_external_tool_paths(paths):
    try:
        with open(EXTERNAL_TOOLS_FILE, 'w', encoding='utf-8') as f:
            json.dump(paths, f, indent=2)
    except Exception:
        pass

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


# ─── DDS smart-format table (from DDS Tool; also seeds Resize Tool's rules) ──
# Defaults below follow the standard Skyrim Community Shaders PBR guidance
# (diffuse/normal/specular/emissive/opacity as RGBA-capable BC7 or BC3;
# roughness/metalness/height/AO as single-channel BC4) - all still editable
# per-rule via the dropdown in either tab, this is just the starting point.
#
# Note on '_s': this app's own established convention (see generate.py /
# BUILD_README) already uses bare '_s' for SUBSURFACE, not "specular" in the
# generic PBR sense - so Specular/Gloss here uses '_spec'/'_specular'/'_gloss'
# instead, to avoid silently reinterpreting an existing suffix's meaning.
_DDS_RULES = [
    (('_n',),                                         'BC7_UNORM',      'Normal'),
    (('_spec', '_specular', '_gloss'),                'BC7_UNORM',      'Specular / Gloss'),
    (('_r', '_rough', '_roughness'),                  'BC4_UNORM',      'Roughness'),
    (('_m', '_mask', '_metal', '_metalness'),         'BC4_UNORM',      'Metalness'),
    (('_env', '_envmask'),                            'BC7_UNORM',      'Environment / Glossiness Mask'),
    (('_g', '_glow', '_emissive'),                    'BC7_UNORM_SRGB', 'Emissive / Glow'),
    (('_opacity', '_alpha', '_trans'),                'BC3_UNORM',      'Opacity / Transparency'),
    (('_ao', '_occlusion'),                           'BC4_UNORM',      'Ambient Occlusion'),
    (('_p', '_height', '_displacement', '_parallax'), 'BC4_UNORM',      'Height / Displacement / Parallax'),
    (('_dtl', '_detail'),                             'BC7_UNORM',      'Detail Map'),
    (('_s', '_subsurface'),                           'BC7_UNORM',      'Subsurface'),
    (('_cnr',),                                       'BC7_UNORM',      'Coat / Multilayer'),
    (('_f',),                                         'BC7_UNORM',      'Fuzz / Sheen'),
    (('_rmaos', '_orm'),                              'BC7_UNORM',      'RMAOS (packed R=AO G=Rough B=Metal)'),
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


# ─── Resize Tool: per-map-type classification (same suffix convention the
# PBR JSON Builder / generate.py already use, so a texture set classifies
# the same way everywhere in the app). Same map types as _DDS_RULES above,
# diffuse-first / rmaos-last for display order in the PER-MAP SIZE list. ──
_RESIZE_MAP_TYPES = [
    ('diffuse',    'Diffuse / Albedo',            ('_d',)),   # explicit suffix; no-suffix also falls here
    ('normal',     'Normal',                      ('_n',)),
    ('specular',   'Specular / Gloss',            ('_spec', '_specular', '_gloss')),
    ('roughness',  'Roughness',                   ('_r', '_rough', '_roughness')),
    ('metalness',  'Metalness',                   ('_m', '_mask', '_metal', '_metalness')),
    ('env',        'Environment / Gloss Mask',    ('_env', '_envmask')),
    ('glow',       'Emissive / Glow',             ('_g', '_glow', '_emissive')),
    ('opacity',    'Opacity / Transparency',      ('_opacity', '_alpha', '_trans')),
    ('ao',         'AO',                          ('_ao', '_occlusion')),
    ('height',     'Height / Parallax',           ('_p', '_height', '_displacement', '_parallax')),
    ('detail',     'Detail Map',                  ('_dtl', '_detail')),
    ('subsurface', 'Subsurface',                  ('_s', '_subsurface')),
    ('coat',       'Coat / Multilayer',           ('_cnr',)),
    ('fuzz',       'Fuzz / Sheen',                ('_f',)),
    ('rmaos',      'RMAOS',                       ('_rmaos', '_orm')),
]
_RESIZE_SIZE_CHOICES = ['Original / No Resize', '4096', '2048', '1024', '512', '256']

_RESIZE_SUFFIX_FLAT = sorted(
    ((suf, key) for key, _label, sufs in _RESIZE_MAP_TYPES for suf in sufs),
    key=lambda pair: -len(pair[0])
)

def resize_classify(filename):
    """Which map-type bucket a filename falls into, by suffix - longest
    suffix wins first so overlapping suffixes never misclassify. No
    recognized suffix -> 'diffuse' (the base/source texture, matching
    generate.py's own convention for an un-suffixed file)."""
    stem = Path(filename).stem.lower()
    for suf, key in _RESIZE_SUFFIX_FLAT:
        if stem.endswith(suf):
            return key
    return 'diffuse'


# ─── texconv helpers ─────────────────────────────────────────────────────────
def _tc_extract(src, out_dir):
    """texconv DDS/any → PNG in out_dir. Returns PNG path or None."""
    tc = texconv_exe()
    if not os.path.isfile(tc):
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([tc, '-nologo', '-y', '-ft', 'png', '-o', str(out_dir), str(src)],
                   capture_output=True)
    out = out_dir / (Path(src).stem + '.png')
    return str(out) if out.exists() else None

def _tc_compress(src, dst, fmt, width=None, height=None):
    """texconv src → DDS at dst with given format. width/height are
    optional - when given, texconv resizes during the same pass (its own
    -w/-h flags) instead of this needing a separate resize step; existing
    callers that don't pass them get the exact same behavior as before."""
    tc = texconv_exe()
    if not os.path.isfile(tc):
        return False
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [tc, '-nologo', '-y', '-m', '0', '-bc', 'd', '-f', fmt]
    if width:
        cmd += ['-w', str(int(width))]
    if height:
        cmd += ['-h', str(int(height))]
    cmd += ['-o', str(dst.parent), str(src)]
    subprocess.run(cmd, capture_output=True)
    expected = dst.parent / (Path(src).stem + '.dds')
    if expected.exists() and expected != dst:
        shutil.move(str(expected), str(dst))
    return dst.exists()


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

    # Quick-launch external apps shown as small icon buttons next to the
    # Input/Output Folder bar. The first three are known apps with default-
    # install guesses; the four 'custom' slots start blank and prompt for
    # both an .exe path AND a short display name the first time they're
    # clicked, so anyone can wire up their own shortcut (Blender, GIMP,
    # whatever) without touching code. Add a fifth fixed app the same way
    # the first three are done, or just raise the range(1, 5) below for
    # more custom slots - nothing else needs to change either way.
    EXTERNAL_TOOLS = [
        {
            'key': 'upscayl', 'label': 'Upscayl', 'icon': '\u2b06', 'custom': False,
            'guesses': [
                r'C:\Program Files\Upscayl\Upscayl.exe',
                os.path.expandvars(r'%LOCALAPPDATA%\Programs\Upscayl\Upscayl.exe'),
            ],
        },
        {
            'key': 'pinta', 'label': 'Pinta', 'icon': '\u270e', 'custom': False,
            'guesses': [
                r'C:\Program Files\Pinta\bin\Pinta.exe',
                os.path.expandvars(r'%LOCALAPPDATA%\Programs\Pinta\bin\Pinta.exe'),
            ],
        },
        {
            'key': 'paintnet', 'label': 'Paint.NET', 'icon': '\U0001f58c', 'custom': False,
            'guesses': [
                r'C:\Program Files\Paint.NET\paintdotnet.exe',
                os.path.expandvars(r'%LOCALAPPDATA%\Programs\paint.net\paintdotnet.exe'),
            ],
        },
    ] + [
        {'key': f'custom{i}', 'label': f'Custom {i}', 'icon': '+', 'custom': True, 'guesses': []}
        for i in range(1, 5)
    ]

    def __init__(self, root):
        self.root = root
        self.root.title(f'Texture Generator v{VERSION}')
        self.root.geometry(f'{self.W}x{self.H}')
        self.root.minsize(960, 680)
        self.root.configure(bg=C['bg'])

        self.input_dir   = tk.StringVar(value=os.getcwd())
        self.output_dir  = tk.StringVar(value=os.getcwd())
        self.texconv_ok  = os.path.isfile(texconv_exe())
        self.last_nm_path = None
        self.external_tool_paths = load_external_tool_paths()

        self._setup_styles()
        self._build_menu()
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
                 font=('Segoe UI', 13, 'bold')).pack(side='left', padx=(18, 6), pady=10)
        tk.Label(hdr, text=f'v{VERSION}', bg='#131313', fg=C['text_dim'],
                 font=('Segoe UI', 9, 'bold')).pack(side='left', pady=10)
        self.bsarch_ok  = os.path.isfile(bsarch_exe())
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

        # Input / Output bar - shared defaults used by every tab that needs
        # a folder (BSA, PBR, Material Generator, JSON Builder, etc. - see
        # BUILD_README.md), so this stays global rather than moving into
        # just the Texture Converters tab; it's shrunk to a fixed-width
        # left column instead, freeing the rest of the row for quick-launch.
        dbar = tk.Frame(self.root, bg=C['surface'], height=54)
        dbar.pack(fill='x'); dbar.pack_propagate(False)

        folder_col = tk.Frame(dbar, bg=C['surface'], width=460)
        folder_col.pack(side='left', fill='y')
        folder_col.pack_propagate(False)

        for i, (lbl, var, fn) in enumerate([
            ('Input Folder:',  self.input_dir,  self._browse_input),
            ('Output Folder:', self.output_dir, self._browse_output),
        ]):
            row = tk.Frame(folder_col, bg=C['surface'])
            row.pack(fill='x', padx=8, pady=(4 if i == 0 else 1, 0))
            tk.Label(row, text=lbl, bg=C['surface'], fg=C['text_dim'],
                     font=('Segoe UI', 8), width=13, anchor='w').pack(side='left', padx=(6,2))
            tk.Entry(row, textvariable=var, bg=C['input'], fg=C['text'],
                     insertbackground=C['text'], relief='flat', font=('Segoe UI', 8), bd=2
                     ).pack(side='left', fill='x', expand=True, padx=2)
            RoundedButton(row, text='…', command=fn,
                          bg=C['accent'], fg='white', font=('Segoe UI', 9, 'bold'),
                          pad=(10, 4)).pack(side='right', padx=(4,6))

        tk.Frame(dbar, bg=C['border'], width=1).pack(side='left', fill='y', pady=8)
        self._build_quick_launch_bar(dbar)

        # Notebook
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill='both', expand=True)

        t1 = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t1, text='  Texture Converters  ')
        self._build_converters_tab(t1)

        t_bsa = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t_bsa, text='  BSA Utilities  ')
        self._build_bsa_tab(t_bsa)

        t2 = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t2, text='  DDS Tool  ')
        self._build_dds_tool_tab(t2)

        t_resize = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t_resize, text='  Resize Tool  ')
        self._build_resize_tool_tab(t_resize)

        t3 = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t3, text='  Normal Map Generator  ')
        self._build_normal_maps_tab(t3)

        t_pmap = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t_pmap, text='  Height Map Generator  ')
        self._build_pmap_tab(t_pmap)

        t4 = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t4, text='  ⚠  PBR Generator  ')
        self._build_pbr_tab(t4)

        t_dual = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t_dual, text='  Dual Layer Builder  ')
        self._build_dual_layer_tab(t_dual)

        t_mat = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t_mat, text='  Material Generator  ')
        self._build_material_tab(t_mat)

        t_json = tk.Frame(self.nb, bg=C['surface'])
        self.nb.add(t_json, text='  PBR JSON Builder  ')
        self._build_json_tab(t_json)

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
        return RoundedButton(parent, text=text, command=cmd,
                              bg=bg or C['accent'], fg=fg,
                              font=font or ('Segoe UI', 9, 'bold'),
                              pad=pad, **kw)

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
    def _bind_wheel_scroll(canvas, frame):
        """Scope mousewheel scrolling to `canvas` while the pointer is
        actually over it (or anything inside it), instead of grabbing it
        globally forever. bind_all('<MouseWheel>') only supports ONE live
        handler for the whole app - every scrollable pane calling it once
        at build time meant whichever was built last silently owned wheel
        scrolling everywhere (that's why, e.g., Material Generator's
        preview grid on the right didn't scroll but its controls on the
        left did - and Dual Layer Builder's grid never scrolled at all,
        since it never even called bind_all). Enter/Leave toggling that
        same global handler on and off, rebound onto every descendant
        widget once the pane's contents exist, means each pane only
        claims it while actually hovered."""
        def _on_wheel(e):
            canvas.yview_scroll(-1 * (e.delta // 120), 'units')

        def _claim(_e=None):
            canvas.bind_all('<MouseWheel>', _on_wheel)

        def _release(_e=None):
            canvas.unbind_all('<MouseWheel>')

        def _bind_recursive(widget):
            widget.bind('<Enter>', _claim, add='+')
            widget.bind('<Leave>', _release, add='+')
            for child in widget.winfo_children():
                _bind_recursive(child)

        canvas.bind('<Enter>', _claim, add='+')
        canvas.bind('<Leave>', _release, add='+')
        # `frame` gets populated with the pane's actual widgets by the
        # caller AFTER this returns, so defer until Tk is idle (i.e. the
        # pane's full contents already exist) before walking its children.
        canvas.after_idle(lambda: _bind_recursive(frame))

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
        TextureGeneratorApp._bind_wheel_scroll(canvas, frame)
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

        self._dl_scroll_canvas, sf = self._make_scrollable(left, C['surface'])
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
            RoundedButton(ch_row, text=lbl, command=lambda i=idx, n=lbl: self._rm_channel(i, n),
                          bg=col, fg='white', font=('Segoe UI', 9, 'bold'),
                          pad=(14, 7)).pack(side='left', padx=6, pady=4)

        self._sep(sf)
        self._section_lbl(sf, 'FILE MANAGEMENT')
        keep_row = tk.Frame(sf, bg=C['surface'])
        keep_row.pack(fill='x', padx=16, pady=(0,6))
        RoundedButton(keep_row, text='Keep Only _n BMP', command=self._keep_n_bmps,
                      bg=C['warn'], fg='#1e1e1e', font=('Segoe UI', 9, 'bold'),
                      pad=(14, 7)).pack(side='left', padx=6, pady=4)
        del_row = tk.Frame(sf, bg=C['surface'])
        del_row.pack(fill='x', padx=16, pady=(0,4))
        tk.Label(del_row, text='Delete all:',
                 bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8)).pack(side='left', padx=(0,8), pady=6)
        for ext in ['BMP','TGA','DDS','PNG']:
            RoundedButton(del_row, text=ext, command=lambda e=ext.lower(): self._rm_ext(e),
                          bg=C['error'], fg='white', font=('Segoe UI', 9, 'bold'),
                          pad=(10, 5)).pack(side='left', padx=4)
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

        src_var = tk.StringVar(value=self.input_dir.get())
        out_var = tk.StringVar(value=self.output_dir.get())
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
            RoundedButton(row, text='×', bg=C['error'], fg='white',
                          font=('Segoe UI', 10, 'bold'), pad=(6, 1),
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
    # TAB 2b – RESIZE TOOL
    # =========================================================================
    def _build_resize_tool_tab(self, parent):
        left_outer = tk.Frame(parent, bg=C['surface'], width=500)
        left_outer.pack(side='left', fill='y')
        left_outer.pack_propagate(False)
        right = tk.Frame(parent, bg=C['panel'])
        right.pack(side='right', fill='both', expand=True)

        _, sf = self._make_scrollable(left_outer, C['surface'])

        self._section_lbl(sf, 'RESIZE TOOL',
            "Batch-resize each map type to its own target size - not one size "
            "for everything. Classification is by filename suffix (same "
            "convention as the PBR JSON Builder), and saving uses the same "
            "rule-based DDS compression as DDS Tool.")

        src_var = tk.StringVar(value=self.input_dir.get())
        out_var = tk.StringVar(value=self.output_dir.get())
        self._folder_row(sf, 'Source Folder', src_var)
        self._folder_row(sf, 'Output Folder', out_var)

        self._sep(sf)
        tk.Label(sf, text='INPUT MODE', bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=16)
        mode_var = tk.StringVar(value='png')
        for val, lbl in [('png', 'PNG → DDS  (compress + resize source art)'),
                         ('dds', 'DDS → DDS  (recompress + resize existing DDS)')]:
            ttk.Radiobutton(sf, text=lbl, variable=mode_var, value=val,
                            style='Dark.TRadiobutton').pack(anchor='w', padx=22, pady=2)

        # ── Per-map-type size ─────────────────────────────────────────────────
        self._sep(sf)
        size_hdr = tk.Frame(sf, bg=C['surface'])
        size_hdr.pack(fill='x', padx=16, pady=(0, 4))
        tk.Label(size_hdr, text='PER-MAP SIZE', bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 9, 'bold')).pack(side='left')
        self._mkbtn(size_hdr, '↺ Reset Sizes', self._rt_reset_sizes,
                    bg=C['panel'], fg=C['text_dim'], pad=(8, 3),
                    font=('Segoe UI', 8)).pack(side='right')

        size_grid = tk.Frame(sf, bg=C['surface'])
        size_grid.pack(fill='x', padx=16, pady=(0, 4))
        size_grid.columnconfigure(0, weight=3)
        size_grid.columnconfigure(1, weight=2)

        self._rt_size_vars = {}
        for r, (key, label, _sufs) in enumerate(_RESIZE_MAP_TYPES):
            tk.Label(size_grid, text=label, bg=C['surface'], fg=C['text'],
                     font=('Segoe UI', 8), anchor='w'
                     ).grid(row=r, column=0, sticky='w', padx=(0, 8), pady=3)
            var = tk.StringVar(value=_RESIZE_SIZE_CHOICES[0])
            self._rt_size_vars[key] = var
            ttk.Combobox(size_grid, textvariable=var, values=_RESIZE_SIZE_CHOICES,
                         state='readonly', font=('Consolas', 8), width=18
                         ).grid(row=r, column=1, sticky='ew', pady=3)
        tk.Label(sf, text='"Original / No Resize" saves at native resolution - '
                          'the file is still processed/compressed, just not resized.',
                 bg=C['surface'], fg=C['text_dim'], font=('Segoe UI', 7),
                 wraplength=460, justify='left').pack(anchor='w', padx=16, pady=(2, 0))

        # ── Format assignment rules (same system + defaults as DDS Tool) ──────
        self._sep(sf)
        hdr_row = tk.Frame(sf, bg=C['surface'])
        hdr_row.pack(fill='x', padx=16, pady=(0, 4))
        tk.Label(hdr_row, text='FORMAT ASSIGNMENT RULES', bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 9, 'bold')).pack(side='left')
        self._mkbtn(hdr_row, '↺ Reset', self._rt_reset_rules,
                    bg=C['panel'], fg=C['text_dim'], pad=(8, 3),
                    font=('Segoe UI', 8)).pack(side='right')
        self._mkbtn(hdr_row, '+ Add Rule', self._rt_add_rule,
                    bg=C['panel'], fg=C['text'], pad=(8, 3),
                    font=('Segoe UI', 8)).pack(side='right', padx=(0, 6))

        col_hdr = tk.Frame(sf, bg=C['surface'])
        col_hdr.pack(fill='x', padx=16, pady=(0, 2))
        col_hdr.columnconfigure(0, weight=3)
        col_hdr.columnconfigure(1, weight=4)
        col_hdr.columnconfigure(2, minsize=24)
        for c, h in enumerate(['Suffix(es)  (comma-separated)', 'DDS Format', '']):
            tk.Label(col_hdr, text=h, bg=C['surface'], fg=C['text_dim'],
                     font=('Segoe UI', 7, 'bold'), anchor='w'
                     ).grid(row=0, column=c, sticky='ew', padx=(0, 4))

        self._rt_rules_container = tk.Frame(sf, bg=C['surface'])
        self._rt_rules_container.pack(fill='x', padx=16, pady=(0, 4))

        self._rt_rule_vars = []
        self._rt_init_rules()
        self._rt_rebuild_rules_ui()

        self._sep(sf)
        def_row = tk.Frame(sf, bg=C['surface'])
        def_row.pack(fill='x', padx=16, pady=(0, 4))
        tk.Label(def_row, text='Default (unmatched files):', bg=C['surface'],
                 fg=C['text_dim'], font=('Segoe UI', 8)).pack(side='left', padx=(0, 8))
        self._rt_default_var = tk.StringVar(value=_DDS_DEFAULT[0])
        ttk.Combobox(def_row, textvariable=self._rt_default_var,
                     values=_DDS_FMT_OPTIONS, state='readonly',
                     font=('Consolas', 8), width=20
                     ).pack(side='left')
        tk.Label(def_row, text='← Albedo / Diffuse', bg=C['surface'],
                 fg=C['text_dim'], font=('Segoe UI', 8)).pack(side='left', padx=(8, 0))

        self._sep(sf)
        self._mkbtn(sf, '▶  Run Resize + Compress',
                    lambda: self._run_resize_tool(src_var, out_var, mode_var),
                    pad=(16, 10), font=('Segoe UI', 10, 'bold')
                    ).pack(fill='x', padx=16, pady=6)
        tk.Frame(sf, bg=C['surface'], height=12).pack()

        tk.Label(right, text='PROCESSING LOG', bg=C['panel'], fg=C['text_dim'],
                 font=('Consolas', 8, 'bold')).pack(anchor='w', padx=10, pady=(10, 2))
        self.resize_log = self._console(right)
        self.resize_log.pack(fill='both', expand=True, padx=8, pady=4)
        self.resize_prog = tk.DoubleVar(value=0)
        ttk.Progressbar(right, variable=self.resize_prog, maximum=100
                        ).pack(fill='x', padx=8, pady=(0, 4))
        self._mkbtn(right, 'Clear', lambda: self._clear_log(self.resize_log),
                    bg=C['panel2'], fg=C['text_dim'], pad=(8, 3),
                    font=('Segoe UI', 8)).pack(anchor='e', padx=8, pady=(0, 8))

    # ── Resize Tool: per-map size management ────────────────────────────────
    def _rt_reset_sizes(self):
        for var in getattr(self, '_rt_size_vars', {}).values():
            var.set(_RESIZE_SIZE_CHOICES[0])

    # ── Resize Tool: format rule management (mirrors DDS Tool's, kept as its
    # own independent state so editing one tab's rules never affects the
    # other) ─────────────────────────────────────────────────────────────────
    def _rt_init_rules(self):
        self._rt_rule_vars = []
        for suffixes, fmt, _ in _DDS_RULES:
            sv = tk.StringVar(value=', '.join(suffixes))
            fv = tk.StringVar(value=fmt)
            self._rt_rule_vars.append((sv, fv))

    def _rt_rebuild_rules_ui(self):
        for w in self._rt_rules_container.winfo_children():
            w.destroy()

        for idx, (sv, fv) in enumerate(self._rt_rule_vars):
            row = tk.Frame(self._rt_rules_container, bg=C['panel'])
            row.pack(fill='x', pady=2)
            row.columnconfigure(0, weight=3)
            row.columnconfigure(1, weight=4)
            row.columnconfigure(2, minsize=26)

            tk.Entry(row, textvariable=sv, bg=C['input'], fg=C['text'],
                     insertbackground=C['text'], relief='flat',
                     font=('Segoe UI', 8)
                     ).grid(row=0, column=0, sticky='ew', padx=(6, 4), pady=5)

            ttk.Combobox(row, textvariable=fv, values=_DDS_FMT_OPTIONS,
                         state='readonly', font=('Consolas', 8), width=18
                         ).grid(row=0, column=1, sticky='ew', padx=(0, 4), pady=5)

            RoundedButton(row, text='×', bg=C['error'], fg='white',
                          font=('Segoe UI', 10, 'bold'), pad=(6, 1),
                          command=lambda i=idx: self._rt_remove_rule(i)
                          ).grid(row=0, column=2, padx=(0, 4), pady=5)

    def _rt_add_rule(self):
        self._rt_rule_vars.append((tk.StringVar(value='_suffix'),
                                   tk.StringVar(value='BC7_UNORM')))
        self._rt_rebuild_rules_ui()

    def _rt_remove_rule(self, idx):
        if 0 <= idx < len(self._rt_rule_vars):
            self._rt_rule_vars.pop(idx)
            self._rt_rebuild_rules_ui()

    def _rt_reset_rules(self):
        self._rt_init_rules()
        self._rt_rebuild_rules_ui()
        if hasattr(self, '_rt_default_var'):
            self._rt_default_var.set(_DDS_DEFAULT[0])

    def _run_resize_tool(self, src_var, out_var, mode_var):
        if not self._need_texconv(self.resize_log):
            return

        rule_snapshot = [(sv.get(), fv.get()) for sv, fv in self._rt_rule_vars]
        default_fmt = getattr(self, '_rt_default_var',
                              type('', (), {'get': lambda s: _DDS_DEFAULT[0]})()
                              ).get()
        size_snapshot = {k: v.get() for k, v in self._rt_size_vars.items()}

        def resolve_fmt(filename):
            stem = Path(filename).stem.lower()
            for suffixes_str, fmt in rule_snapshot:
                for s in [x.strip() for x in suffixes_str.split(',') if x.strip()]:
                    if stem.endswith(s):
                        return fmt
            return default_fmt

        def run():
            self._clear_log(self.resize_log); self.resize_prog.set(0)
            src = Path(src_var.get())
            out = Path(out_var.get())
            ext = '.dds' if mode_var.get() == 'dds' else '.png'
            files = [f for f in src.rglob(f'*{ext}') if f.is_file()]
            if not files:
                self._log(self.resize_log, f'No {ext.upper()} files in: {src}', C['warn'])
                return

            mode_label = 'DDS→DDS recompress' if mode_var.get() == 'dds' else 'PNG→DDS compress'
            self._log(self.resize_log,
                      f'Mode:   {mode_label}\n'
                      f'Source: {src}\n'
                      f'Output: {out}\n'
                      f'Found:  {len(files)} file(s)\n')
            ok = fail = 0
            for i, f in enumerate(files, 1):
                cat = resize_classify(f.name)
                size_choice = size_snapshot.get(cat, _RESIZE_SIZE_CHOICES[0])
                fmt = resolve_fmt(f.name)
                rel = f.relative_to(src)
                out_dir = out / rel.parent
                out_dir.mkdir(parents=True, exist_ok=True)
                dst = out_dir / (f.stem + '.dds')

                w = h = None
                size_label = 'original'
                if size_choice != _RESIZE_SIZE_CHOICES[0]:
                    w = h = int(size_choice)
                    size_label = f'{w}x{h}'

                self._log(self.resize_log, f'  {rel}  [{cat}]  →  {fmt}  @ {size_label}')
                try:
                    if _tc_compress(str(f), str(dst), fmt, width=w, height=h):
                        self._log(self.resize_log, '    ✓', C['success']); ok += 1
                    else:
                        self._log(self.resize_log, '    ✗ texconv failed', C['error']); fail += 1
                except Exception as e:
                    self._log(self.resize_log, f'    ✗ {e}', C['error']); fail += 1
                self.root.after(0, lambda p=i / len(files) * 100: self.resize_prog.set(p))

            self.resize_prog.set(100)
            self._log(self.resize_log,
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
        RoundedButton(fr, text='Browse…', command=_pick_bsa,
                      bg=C['accent'], fg='white', font=('Segoe UI', 8, 'bold'),
                      pad=(10, 5)).pack(side='right')

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

        self._bsa_unpack_var = tk.StringVar(value=self.output_dir.get())
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
        RoundedButton(out_row, text='…', command=_pick_bsa_out,
                      bg=C['accent'], fg='white', font=('Segoe UI', 9, 'bold'),
                      pad=(10, 4)).pack(side='right')

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
                    [bsarch_exe()] + args,
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
        RoundedButton(row, text='…', command=_browse,
                      bg=C['accent'], fg='white', font=('Segoe UI', 9, 'bold'),
                      pad=(10, 4)).pack(side='right')

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
                 variable=self.nm_scale, bg=C['accent'], fg=C['text'],
                 troughcolor=C['input'], highlightthickness=0, activebackground=C['accent_hi'],
                 showvalue=False, sliderlength=18, sliderrelief='raised', bd=1,
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
            try: os.startfile(d)
            except Exception: subprocess.Popen(['explorer', d])

    # ── Quick-launch external apps (Upscayl, Pinta, Paint.NET, + custom) ────
    def _build_quick_launch_bar(self, parent):
        """Small, extensible row of icon buttons that launch external apps
        directly. Left-click launches (browsing to the exe once if it
        can't be found automatically or from a previously saved path -
        custom slots also ask for a short display name at that point;
        that path/name is then remembered in external_tools.json).
        Right-click changes the remembered path (and name, for custom
        slots). Rebuilds itself from EXTERNAL_TOOLS - add an app there,
        not here, to add a new fixed icon."""
        bar = tk.Frame(parent, bg=C['surface'])
        bar.pack(side='left', fill='both', expand=True, padx=(10, 8), pady=8)
        self.quick_launch_bar = bar
        self._tool_buttons = {}
        self._tool_tooltips = {}

        tk.Label(bar, text='QUICK LAUNCH', bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 7, 'bold')).pack(side='left', padx=(0, 10))

        for tool in self.EXTERNAL_TOOLS:
            btn = RoundedButton(bar, text=self._tool_glyph(tool),
                                 command=lambda t=tool: self._launch_external_tool(t),
                                 bg=C['panel2'], fg=C['text_bright'], font=('Segoe UI', 12, 'bold'),
                                 pad=(6, 4), width=34)
            btn.pack(side='left', padx=3)
            btn.bind('<Button-3>', lambda e, t=tool: self._change_tool_path(t))
            self._tool_buttons[tool['key']] = btn
            self._tool_tooltips[tool['key']] = _Tooltip(btn, self._tool_tooltip_text(tool))

    def _tool_glyph(self, tool):
        """Fixed apps always show their own icon glyph. Blank custom slots
        show '+' until configured, then show the first letter of whatever
        name the user gave it - RoundedButton is single-glyph, so this is
        the compact way to make a configured custom slot recognizable at
        a glance."""
        if tool.get('custom'):
            entry = self.external_tool_paths.get(tool['key'])
            if isinstance(entry, dict) and entry.get('label'):
                return entry['label'][0].upper()
            return tool['icon']
        return tool['icon']

    def _tool_tooltip_text(self, tool):
        label = self.get_tool_label(tool)
        return f'Launch {label}  (right-click to set/change its .exe path{" and name" if tool.get("custom") else ""})'

    def _refresh_tool_button(self, tool):
        btn = self._tool_buttons.get(tool['key'])
        if btn:
            btn.configure(text=self._tool_glyph(tool))
        tip = self._tool_tooltips.get(tool['key'])
        if tip:
            tip.text = self._tool_tooltip_text(tool)

    def get_tool_label(self, tool):
        entry = self.external_tool_paths.get(tool['key'])
        if isinstance(entry, dict) and entry.get('label'):
            return entry['label']
        return tool['label']

    def get_tool_path(self, key):
        entry = self.external_tool_paths.get(key)
        if isinstance(entry, dict):
            return entry.get('path')
        if isinstance(entry, str):   # tolerate a bare-string entry too
            return entry
        return None

    def set_tool_path(self, key, path, label=None):
        entry = self.external_tool_paths.get(key)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry['path'] = path
        if label is not None:
            entry['label'] = label
        self.external_tool_paths[key] = entry
        save_external_tool_paths(self.external_tool_paths)

    def _resolve_tool_path(self, tool):
        """Saved path first, then default-install guesses, else None."""
        saved = self.get_tool_path(tool['key'])
        if saved and os.path.isfile(saved):
            return saved
        return next((g for g in tool['guesses'] if os.path.isfile(g)), None)

    def _launch_external_tool(self, tool):
        path = self._resolve_tool_path(tool)
        if not path:
            if tool.get('custom'):
                dlg = _CustomToolDialog(self.root, initial_name=self._current_custom_label(tool))
                if not dlg.result:
                    return
                label, path = dlg.result
            else:
                path = filedialog.askopenfilename(
                    title=f"Locate the .exe for {tool['label']}",
                    filetypes=[('Executable', '*.exe'), ('All files', '*.*')])
                if not path:
                    return
                label = None
            self.set_tool_path(tool['key'], path, label)
            self._refresh_tool_button(tool)
        try:
            os.startfile(path)
        except Exception:
            try:
                subprocess.Popen([path])
            except Exception as e:
                messagebox.showerror(f"Couldn't launch {self.get_tool_label(tool)}", str(e))

    def _change_tool_path(self, tool):
        if tool.get('custom'):
            dlg = _CustomToolDialog(self.root, initial_name=self._current_custom_label(tool),
                                     initial_path=self.get_tool_path(tool['key']) or '')
            if not dlg.result:
                return
            label, path = dlg.result
        else:
            path = filedialog.askopenfilename(
                title=f"Locate the .exe for {tool['label']}",
                filetypes=[('Executable', '*.exe'), ('All files', '*.*')])
            if not path:
                return
            label = None
        self.set_tool_path(tool['key'], path, label)
        self._refresh_tool_button(tool)
        messagebox.showinfo(label or tool['label'], f'Path saved:\n{path}')

    def _current_custom_label(self, tool):
        """Only pre-fill the name field with something the user actually
        typed before - not the generic 'Custom 1' placeholder."""
        label = self.get_tool_label(tool)
        return '' if label == tool['label'] else label

    # ── Menu bar (File / View / Help) ────────────────────────────────────────
    def _build_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label='Set Input Folder…', command=self._browse_input)
        file_menu.add_command(label='Set Output Folder…', command=self._browse_output)
        file_menu.add_separator()
        file_menu.add_command(label='Open Output Folder', command=self._open_out)
        file_menu.add_separator()
        file_menu.add_command(label='Exit', command=self.root.destroy)
        menubar.add_cascade(label='File', menu=file_menu)

        self._always_on_top = tk.BooleanVar(value=False)
        self._show_quick_launch = tk.BooleanVar(value=True)
        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_checkbutton(
            label='Always on Top', variable=self._always_on_top,
            command=lambda: self.root.attributes('-topmost', self._always_on_top.get()))
        view_menu.add_checkbutton(
            label='Show Quick Launch Bar', variable=self._show_quick_launch,
            command=self._toggle_quick_launch_bar)
        view_menu.add_separator()
        view_menu.add_command(label='Reset Window Size',
                               command=lambda: self.root.geometry(f'{self.W}x{self.H}'))
        menubar.add_cascade(label='View', menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label='Tool Status (PIL / texconv / BSArch / quick-launch apps)',
                               command=self._show_tool_status)
        help_menu.add_command(label='Open Build Guide (BUILD_README.md)',
                               command=self._open_build_readme)
        help_menu.add_separator()
        help_menu.add_command(label='About Texture Generator', command=self._show_about)
        menubar.add_cascade(label='Help', menu=help_menu)

        self.root.config(menu=menubar)

    def _toggle_quick_launch_bar(self):
        if self._show_quick_launch.get():
            self.quick_launch_bar.pack(side='left', fill='both', expand=True, padx=(10, 8), pady=8)
        else:
            self.quick_launch_bar.pack_forget()

    def _show_tool_status(self):
        lines = [
            'PIL          : available',
            f'texconv.exe  : {"found" if self.texconv_ok else "NOT FOUND"}',
            f'BSArch.exe   : {"found" if getattr(self, "bsarch_ok", False) else "NOT FOUND"}',
            '',
        ]
        for tool in self.EXTERNAL_TOOLS:
            p = self._resolve_tool_path(tool)
            label = self.get_tool_label(tool)
            status = p if p else ('not set - click its icon or right-click to set it' if not tool.get('custom')
                                   else 'blank slot - click to name it and set an .exe')
            lines.append(f'{label:<12}: {status}')
        messagebox.showinfo('Tool Status', '\n'.join(lines))

    def _open_build_readme(self):
        path = os.path.join(app_config_dir(), 'BUILD_README.md')
        if os.path.isfile(path):
            try: os.startfile(path)
            except Exception: subprocess.Popen(['notepad', path])
        else:
            messagebox.showinfo('Build Guide', 'BUILD_README.md was not found next to this app.')

    def _show_about(self):
        messagebox.showinfo(
            'About',
            f'Texture Generator  v{VERSION}\n\n'
            'Batch texture / material / PBR tooling, with quick-launch\n'
            'bridges to Upscayl and Pinta.\n\n'
            f'PIL: available    texconv: {"found" if self.texconv_ok else "missing"}    '
            f'BSArch: {"found" if getattr(self, "bsarch_ok", False) else "missing"}')

    # =========================================================================
    # TAB 4 – PBR GENERATION
    # =========================================================================
    def _build_pbr_tab(self, parent):
        banner = tk.Frame(parent, bg='#2a1800', height=30)
        banner.pack(fill='x'); banner.pack_propagate(False)
        tk.Label(banner,
                 text='⚠  AI-assisted PBR generation — review all outputs before production use.',
                 bg='#2a1800', fg=C['exp'],
                 font=('Segoe UI', 8,'bold')).pack(side='left', padx=14, pady=6)
        if not _PBR_OK:
            tk.Label(parent,
                     text=f'pbr_engine.py failed to load:\n{_PBR_ERR}',
                     bg=C['surface'], fg=C['error'],
                     font=('Segoe UI', 10), justify='left').pack(expand=True)
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

    def _pbr_layout(self, parent, title, desc):
        container = tk.Frame(parent, bg=C['bg'])
        container.pack(fill='both', expand=True)
        container.columnconfigure(0, weight=1, minsize=360)
        container.columnconfigure(1, weight=2)
        container.rowconfigure(0, weight=1)

        left_wrapper = tk.Frame(container, bg=C['surface'])
        left_wrapper.grid(row=0, column=0, sticky='nsew', padx=(0,1))
        left_wrapper.columnconfigure(0, weight=1)
        left_wrapper.rowconfigure(0, weight=1)

        left_canvas, left = self._make_scrollable(left_wrapper, C['surface'])

        right = tk.Frame(container, bg=C['panel'])
        right.grid(row=0, column=1, sticky='nsew')
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        tk.Label(right, text='PROCESSING LOG', bg=C['panel'], fg=C['text_dim'],
                 font=('Consolas', 8,'bold')).pack(anchor='w', padx=10, pady=(10,2))
        log_w = self._console(right)
        log_w.pack(fill='both', expand=True, padx=8, pady=4)

        bot = tk.Frame(right, bg=C['panel'])
        bot.pack(fill='x', padx=8, pady=(0,8))
        pv = tk.DoubleVar(value=0)
        ttk.Progressbar(bot, variable=pv, maximum=100).pack(side='left', fill='x', expand=True, padx=(0,8))
        self._mkbtn(bot, 'Clear', lambda: self._clear_log(log_w),
                    bg=C['panel2'], fg=C['text_dim'], pad=(8,3),
                    font=('Segoe UI', 8)).pack(side='right')

        tk.Label(left, text=title, bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 10,'bold')).pack(anchor='w', padx=16, pady=(14,1))
        tk.Label(left, text=desc, bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8), wraplength=380, justify='left'
                 ).pack(anchor='w', padx=16, pady=(0,10))

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
                 variable=var, bg=C['accent'], fg=C['text'],
                 activebackground=C['accent_hi'], troughcolor=C['input'],
                 highlightthickness=0, showvalue=False, sliderlength=16,
                 sliderrelief='raised', bd=1,
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

    def _run_ps_script(self, script_rel, ps_args, log_w, pv, done_msg='Done.', on_complete=None):
        """Runs a bundled PowerShell script (Step1.ps1 / Step2.ps1), streaming
        its output into log_w and parsing __SKYKING_TOTAL__/__SKYKING_PROGRESS__
        markers for the progress bar — same conventions as _run_pbr_op above,
        just backed by a subprocess instead of an in-process Python call.
        on_complete(success: bool) is invoked on the Tk thread once the
        process exits, so callers can chain steps without guessing timing."""
        def _log(msg, c=None):
            fg = {'success': C['success'], 'warn': C['warn'], 'error': C['error']}.get(c)
            self.root.after(0, lambda: self._log(log_w, msg, fg))
        def _prog(done, total):
            self.root.after(0, lambda: pv.set(done / total * 100 if total else 0))

        def run():
            self._clear_log(log_w); pv.set(0)
            ps = powershell_exe()
            script = resource_path(script_rel)
            if not os.path.isfile(ps):
                _log(f'powershell.exe not found (expected at {ps}). Rebuild to bundle it.', 'error')
                if on_complete: self.root.after(0, lambda: on_complete(False))
                return
            if not os.path.isfile(script):
                _log(f'{script_rel} not found (expected at {script}). Rebuild to bundle it.', 'error')
                if on_complete: self.root.after(0, lambda: on_complete(False))
                return
            cmd = [ps, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script] + [str(a) for a in ps_args]
            try:
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, creationflags=creationflags)
                for line in proc.stdout:
                    line = line.rstrip('\n')
                    if not line:
                        continue
                    if line.startswith('__SKYKING_TOTAL__='):
                        continue  # total alone isn't actionable; wait for a progress line
                    if line.startswith('__SKYKING_PROGRESS__='):
                        try:
                            done, tot = line.split('=', 1)[1].split('/')
                            _prog(int(done), int(tot))
                        except ValueError:
                            pass
                        continue
                    color = 'error' if line.startswith(('[ERROR]', '❌', 'ERROR')) else \
                            ('warn' if line.startswith(('[WARN]',)) else
                            ('success' if line.startswith(('[DONE]', '✅', '✔')) else None))
                    _log(line, color)
                proc.wait()
                ok = (proc.returncode == 0)
                if ok:
                    self.root.after(0, lambda: pv.set(100))
                    self.root.after(0, lambda: self._log(log_w, f'\n{done_msg}', C['success']))
                else:
                    self.root.after(0, lambda: self._log(log_w,
                        f'\nScript exited with code {proc.returncode}.', C['error']))
                if on_complete:
                    self.root.after(0, lambda: on_complete(ok))
            except Exception as e:
                self.root.after(0, lambda: self._log(log_w, f'\nError: {e}', C['error']))
                if on_complete:
                    self.root.after(0, lambda: on_complete(False))
        threading.Thread(target=run, daemon=True).start()
        if not self.texconv_ok:
            messagebox.showerror('texconv Missing',
                'texconv.exe not found. Rebuild to bundle it inside the exe.')
            return False
        return True

    def _pbr_builder_tab(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'PBR BUILDER',
            'Convert loose PBR maps (albedo, normal, roughness, metalness, AO, height) '
            'into packed Community Shaders DDS files.')
        src_var = tk.StringVar(value=self.input_dir.get())
        out_var = tk.StringVar(value=self.output_dir.get())
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
                    lambda: self._need_texconv(log_w) and self._run_pbr_op(
                        _pe.run_build_pbr, log_w, pv,
                        src_var.get(), out_var.get(), flip_var.get(),
                        done_msg='PBR Builder complete.'),
                    pad=(16,10), font=('Segoe UI', 10,'bold')).pack(fill='x', padx=16, pady=6)

    def _pbr_parallax_tab(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'PARALLAX GENERATOR',
            'Generate Complex Parallax _m and/or CS PBR textures from diffuse + normal. '
            'Height is derived via FFT if not present in source.')
        src_var = tk.StringVar(value=self.input_dir.get())
        out_var = tk.StringVar(value=self.output_dir.get())
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
            if not self._need_texconv(log_w): return
            excl = [x.strip() for x in excl_var.get().split(',') if x.strip()]
            cfg  = {'default': {k: v.get() for k, v in sliders.items()}, 'exclude': excl}
            self._run_pbr_op(_pe.run_generate_parallax, log_w, pv,
                             src_var.get(), out_var.get(), mode_var.get(),
                             config_override=cfg, done_msg='Parallax generation complete.')
        self._mkbtn(left, '▶  Generate Parallax Textures', run,
                    pad=(16,10), font=('Segoe UI', 10,'bold')).pack(fill='x', padx=16, pady=6)

    def _build_json_tab(self, parent):
        banner = tk.Frame(parent, bg='#132a1e', height=30)
        banner.pack(fill='x'); banner.pack_propagate(False)
        tk.Label(banner,
                 text='🗒  PBR JSON BUILDER  —  two independent tools, side by side. '
                      'Left: Python/keyword-driven scanner.  Right: PowerShell scaffold-then-fill workflow.  Drag divider to resize.',
                 bg='#132a1e', fg=C['success'], font=('Segoe UI', 8, 'bold')
                 ).pack(side='left', padx=14, pady=6)

        body = tk.Frame(parent, bg=C['bg'])
        body.pack(fill='both', expand=True)

        pw = tk.PanedWindow(body, orient='horizontal', bg=C['border'], sashwidth=6, sashrelief='flat', handlepad=20, handlesize=8, showhandle=True)
        pw.pack(fill='both', expand=True, padx=2, pady=2)

        left_half = tk.Frame(pw, bg=C['bg'])
        right_half = tk.Frame(pw, bg=C['bg'])
        pw.add(left_half, minsize=380, width=700, stretch='always')
        pw.add(right_half, minsize=380, width=700, stretch='always')

        self._json_left_pane(left_half)
        self._json_right_pane(right_half)

    def _json_left_pane(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'PYTHON  ·  KEYWORD-DRIVEN',
            'Scans a mod folder for texture sets (needs at least diffuse + normal — '
            'height/rmaos/glow/fuzz/subsurface/coat maps are all optional and '
            'auto-detected) and writes a PBRNIFPatcher JSON. Extra config lives in '
            'config.json (keyword + per-file overrides).')
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
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=16)
        tk.Label(left,
                 text='emissive / parallax / subsurface / multilayer / fuzz are auto-'
                      'detected per texture from _g/_p/_s/_cnr/_f maps — these sliders '
                      'only set the static fallback values.',
                 bg=C['surface'], fg=C['text_dim'], font=('Segoe UI', 7),
                 wraplength=390, justify='left').pack(anchor='w', padx=16, pady=(0, 6))
        s_vars = {
            'specular_level':     self._slider_row(left, 'Specular Level',     0.0, 1.0, 0.01, 0.04, 18),
            'roughness_scale':    self._slider_row(left, 'Roughness Scale',    0.0, 2.0, 0.05, 1.0,  18),
            'subsurface_opacity': self._slider_row(left, 'Subsurface Opacity', 0.0, 1.0, 0.05, 1.0,  18),
            'smooth_angle':       self._slider_row(left, 'Smooth Angle',       0, 180, 1, 75,         18),
        }
        self._sep(left)
        def run():
            cfg = {'defaults': {k: v.get() for k, v in s_vars.items()}}
            self._run_pbr_op(_pe.run_generate_json, log_w, pv,
                             mod_var.get(), name_var.get(),
                             config_override=cfg, done_msg='JSON generation complete.')
        self._mkbtn(left, '▶  Generate PBR JSON', run,
                    pad=(16, 10), font=('Segoe UI', 10, 'bold')).pack(fill='x', padx=16, pady=6)

    def _json_right_pane(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'POWERSHELL  ·  SCAFFOLD + FILL',
            'Two-step workflow: Step 1 scaffolds a template JSON per diffuse texture '
            'found under Textures\\PBR. Step 2 fills each one in from whichever '
            '_d/_g/_f/_p/_s/_cnr maps actually sit next to it (and, if the JSON was '
            'named "..._d", renames the diffuse file to drop that suffix).')
        mod_var = tk.StringVar(value=self.input_dir.get())
        self._folder_row(left, 'Mod Folder', mod_var)
        self._sep(left)
        tk.Label(left, text='STEP 2 SETTINGS', bg=C['surface'], fg=C['accent'],
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=16)
        s_vars = {
            'SpecularLevel':      self._slider_row(left, 'Specular Level',      0.0, 1.0, 0.01,  0.04,  18),
            'RoughnessScale':     self._slider_row(left, 'Roughness Scale',     0.0, 2.0, 0.05,  1.0,   18),
            'SubsurfaceOpacity':  self._slider_row(left, 'Subsurface Opacity',  0.0, 1.0, 0.05,  1.0,   18),
            'DisplacementScale':  self._slider_row(left, 'Displacement Scale',  0.0, 2.0, 0.05,  1.0,   18),
            'MultilayerDisplacementScale':
                                  self._slider_row(left, 'Multilayer Displacement', 0.0, 3.0, 0.05, 2.0, 18),
            'CoatStrength':       self._slider_row(left, 'Coat Strength',       0.0, 1.0, 0.05,  1.0,   18),
            'CoatRoughness':      self._slider_row(left, 'Coat Roughness',      0.0, 1.0, 0.05,  1.0,   18),
            'CoatSpecularLevel':  self._slider_row(left, 'Coat Specular Level', 0.0, 0.2, 0.002, 0.018, 18),
        }
        self._sep(left)

        def _step2_args():
            args = ['-ModRoot', mod_var.get()]
            for name, var in s_vars.items():
                args += [f'-{name}', str(var.get())]
            return args

        def run_step1(on_complete=None):
            self._run_ps_script('Step1.ps1', ['-ModRoot', mod_var.get()], log_w, pv,
                                done_msg='Step 1 complete — stub JSONs scaffolded.',
                                on_complete=on_complete)
        def run_step2(on_complete=None):
            self._run_ps_script('Step2.ps1', _step2_args(), log_w, pv,
                                done_msg='Step 2 complete — JSONs filled in.',
                                on_complete=on_complete)
        def run_both():
            run_step1(on_complete=lambda ok: run_step2() if ok else None)

        self._mkbtn(left, '▶  Run Both Steps', run_both,
                    pad=(16, 10), font=('Segoe UI', 10, 'bold')).pack(fill='x', padx=16, pady=(6, 3))
        br = tk.Frame(left, bg=C['surface']); br.pack(fill='x', padx=16, pady=(0, 6))
        self._mkbtn(br, 'Step 1 only', lambda: run_step1(), bg=C['panel2'], fg=C['text'],
                    pad=(10, 6), font=('Segoe UI', 8)).pack(side='left', fill='x', expand=True, padx=(0, 4))
        self._mkbtn(br, 'Step 2 only', lambda: run_step2(), bg=C['panel2'], fg=C['text'],
                    pad=(10, 6), font=('Segoe UI', 8)).pack(side='left', fill='x', expand=True, padx=(4, 0))

    def _pbr_conv_to_pbr_tab(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'COMPLEX PARALLAX → CS PBR',
            'Convert Complex Parallax sets (_m) to Community Shaders PBR format.\n'
            'Needs: <name>.dds  +  <name>_n.dds  +  <name>_m.dds')
        src_var = tk.StringVar(value=self.input_dir.get())
        out_var = tk.StringVar(value=self.output_dir.get())
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
                    lambda: self._need_texconv(log_w) and self._run_pbr_op(
                        _pe.run_convert_to_pbr, log_w, pv,
                        src_var.get(), out_var.get(),
                        done_msg='Complex → PBR complete.'),
                    pad=(16,10), font=('Segoe UI', 10,'bold')).pack(fill='x', padx=16, pady=6)

    def _pbr_conv_to_complex_tab(self, parent):
        left, log_w, pv = self._pbr_layout(parent, 'CS PBR → COMPLEX PARALLAX',
            'Convert CS PBR texture sets to Complex Parallax _m format.\n'
            'Needs: diffuse  +  _n normal  +  _p height  (optional: _rmaos)')
        src_var = tk.StringVar(value=self.input_dir.get())
        out_var = tk.StringVar(value=self.output_dir.get())
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
                    lambda: self._need_texconv(log_w) and self._run_pbr_op(
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
        RoundedButton(src_r, text='Browse…', command=_pick,
                      bg=C['accent'], fg='white', font=('Segoe UI', 8, 'bold'),
                      pad=(10, 5)).pack(side='right')

        self._mkbtn(sf, '🖼  Load & Preview Source', self._mat_load_preview,
                    bg=C['panel'], fg=C['text'], pad=(16, 6),
                    font=('Segoe UI', 9)).pack(fill='x', padx=16, pady=4)

        # ── Output ────────────────────────────────────────────────────────────
        self._sep(sf)
        self._section_lbl(sf, 'OUTPUT')
        self._mat_out_var = tk.StringVar(value=self.output_dir.get())
        self._folder_row(sf, 'Output Folder', self._mat_out_var)

        fmt_r = tk.Frame(sf, bg=C['surface'])
        fmt_r.pack(fill='x', padx=16, pady=(4, 2))
        tk.Label(fmt_r, text='Format:', bg=C['surface'], fg=C['text'],
                 font=('Segoe UI', 8)).pack(side='left', padx=(0, 8))
        self._mat_fmt_var = tk.StringVar(value='PNG')
        for fmt in ['PNG', 'TGA', 'BMP']:
            ttk.Radiobutton(fmt_r, text=fmt, variable=self._mat_fmt_var, value=fmt,
                            style='Dark.TRadiobutton').pack(side='left', padx=(0, 10))

        # ── AI Depth (High Quality) ─────────────────────────────────────────
        self._sep(sf)
        self._section_lbl(sf, 'AI DEPTH - HIGH QUALITY (like reference)',
            'Replaces luminance height with AI depth. Gives you that smooth sculpted depth like your reference image, then normal is derived from it. Requires torch + transformers. Falls back to luminance if not installed.')

        ai_card = tk.Frame(sf, bg=C['panel'])
        ai_card.pack(fill='x', padx=16, pady=3)

        self._mat_ai_enabled = tk.BooleanVar(value=False)
        self._mat_ai_model_var = tk.StringVar(value='depth-anything-small')
        self._mat_ai_detail_var = tk.DoubleVar(value=0.15)
        self._mat_ai_strength_var = tk.DoubleVar(value=1.0)

        hdr = tk.Frame(ai_card, bg=C['panel'])
        hdr.pack(fill='x', padx=8, pady=(6,2))
        ttk.Checkbutton(hdr, text='', variable=self._mat_ai_enabled, style='Panel.TCheckbutton').pack(side='left')
        tk.Label(hdr, text='USE AI DEPTH', bg=C['panel'], fg='#ffcc00', font=('Segoe UI', 9, 'bold')).pack(side='left', padx=(2,8))
        if not _AI_OK:
            tk.Label(hdr, text='(torch/transformers not installed - will use luminance)', bg=C['panel'], fg=C['warn'], font=('Segoe UI', 7)).pack(side='left')

        # Model row
        mrow = tk.Frame(ai_card, bg=C['panel'])
        mrow.pack(fill='x', padx=8, pady=2)
        tk.Label(mrow, text='Model:', bg=C['panel'], fg=C['text'], font=('Segoe UI', 8), width=12, anchor='w').pack(side='left')
        try:
            models = _lazy_load_ai().get_available_models() if _lazy_load_ai() else ['depth-anything-small', 'depth-anything-base', 'marigold-v1']
        except:
            models = ['depth-anything-small', 'depth-anything-base', 'marigold-v1']
        cb = ttk.Combobox(mrow, textvariable=self._mat_ai_model_var, values=models, state='readonly', font=('Segoe UI', 8), width=22)
        cb.pack(side='left', padx=(4,0))
        tk.Label(mrow, text='small=fast CPU, marigold=quality GPU (like lucidrains Unet)', bg=C['panel'], fg=C['text_dim'], font=('Segoe UI', 7)).pack(side='left', padx=(6,0))

        # Detail blend row
        drow = tk.Frame(ai_card, bg=C['panel'])
        drow.pack(fill='x', padx=8, pady=2)
        tk.Label(drow, text='Detail Blend:', bg=C['panel'], fg=C['text'], font=('Segoe UI', 7), width=12, anchor='w').pack(side='left')
        tk.Scale(drow, from_=0.0, to=0.5, resolution=0.05, orient='horizontal', variable=self._mat_ai_detail_var, bg=C['panel'], fg=C['text'], highlightthickness=0, length=140, font=('Segoe UI', 7)).pack(side='left')
        tk.Label(drow, text='0= smooth like ref, 0.15= preserve engravings', bg=C['panel'], fg=C['text_dim'], font=('Segoe UI', 7)).pack(side='left', padx=(4,0))

        # Strength row
        srow = tk.Frame(ai_card, bg=C['panel'])
        srow.pack(fill='x', padx=8, pady=2)
        tk.Label(srow, text='AI Strength:', bg=C['panel'], fg=C['text'], font=('Segoe UI', 7), width=12, anchor='w').pack(side='left')
        tk.Scale(srow, from_=0.2, to=2.0, resolution=0.1, orient='horizontal', variable=self._mat_ai_strength_var, bg=C['panel'], fg=C['text'], highlightthickness=0, length=140, font=('Segoe UI', 7)).pack(side='left')

        # ── Map settings ──────────────────────────────────────────────────────
        self._sep(sf)
        self._section_lbl(sf, 'MAP SETTINGS',
            'Enable / disable maps and adjust parameters.')

        self._mat_enabled  = {}
        self._mat_svars    = {}   # {map_name: {param: DoubleVar/BooleanVar}}

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
                            style='Panel.TCheckbutton').pack(side='left')
            tk.Label(hdr, text=label, bg=C['panel'], fg=col,
                     font=('Segoe UI', 9, 'bold')).pack(side='left', padx=(2, 8))
            for tog_lbl, tog_key in toggles:
                tv = tk.BooleanVar(value=False)
                ttk.Checkbutton(hdr, text=tog_lbl, variable=tv,
                                style='Panel.TCheckbutton').pack(side='right', padx=(4, 0))
                self._mat_svars[map_name][tog_key] = tv

            # Sliders
            for s_lbl, s_key, lo, hi, res, default in sliders:
                sv = self._mat_compact_slider(card, s_lbl, lo, hi, res, default)
                self._mat_svars[map_name][s_key] = sv

            tk.Frame(card, bg=C['panel'], height=5).pack()

        # ── RMAOS packer ─────────────────────────────────────────────────────────
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
                    lambda: self._open_material_preview(source='material_generator'),
                    bg='#1a3a2a', fg=C['success'],
                    pad=(14, 10), font=('Segoe UI', 10, 'bold')
                    ).pack(fill='x', padx=10, pady=(0, 10))


        # ── Right: 2-column preview grid ──────────────────────────────────────
        rc = tk.Canvas(right, bg=C['bg'], highlightthickness=0)
        rsb = ttk.Scrollbar(right, orient='vertical', command=rc.yview)
        gf = tk.Frame(rc, bg=C['bg'])
        gf.bind('<Configure>', lambda e: rc.configure(scrollregion=rc.bbox('all')))
        gw = rc.create_window((0, 0), window=gf, anchor='nw')
        rc.configure(yscrollcommand=rsb.set)
        rc.bind('<Configure>', lambda e, w=gw: rc.itemconfig(w, width=e.width))
        rc.pack(side='left', fill='both', expand=True)
        rsb.pack(side='right', fill='y')
        self._bind_wheel_scroll(rc, gf)

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
    def _mat_compact_slider(self, parent, label, lo, hi, res, default):
        """One compact label+scale+value row inside a map settings card."""
        var = tk.DoubleVar(value=default)
        row = tk.Frame(parent, bg=C['panel'])
        row.pack(fill='x', padx=8, pady=1)
        tk.Label(row, text=label, bg=C['panel'], fg=C['text'],
                 font=('Segoe UI', 7), width=10, anchor='w').pack(side='left')
        val_lbl = tk.Label(row, text=f'{default:.2f}', bg=C['panel'],
                           fg=C['text_bright'], font=('Consolas', 7), width=6)
        val_lbl.pack(side='right')
        tk.Scale(row, from_=lo, to=hi, resolution=res, orient='horizontal',
                 variable=var, bg=C['accent'], fg=C['text'],
                 troughcolor=C['input'], highlightthickness=0, activebackground=C['accent_hi'],
                 showvalue=False, sliderlength=12, sliderrelief='raised', bd=1,
                 command=lambda v, l=val_lbl: l.config(text=f'{float(v):.2f}')
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

    def _open_material_preview(self, source='material_generator'):
        """Open (or focus) the 3D material preview window, loaded with maps
        from whichever tab asked for it (source='material_generator' or
        'dual_layer')."""
        global _preview_win
        if not _MP_OK:
            messagebox.showerror('Missing Module',
                f'material_preview.py failed to load:\n{_MP_ERR}')
            return
        source_changed = getattr(self, '_preview_source', None) != source
        self._preview_source = source
        if _preview_win is None or not (_preview_win.win and
                                         _preview_win.win.winfo_exists()):
            _preview_win = _mp.MaterialPreviewWindow(self)
        elif source_changed:
            # Same window, different tab's maps — force a reload on next render.
            _preview_win._maps_dirty = True
            _preview_win._queue_render()
        _preview_win.open()

    def get_preview_maps(self):
        """Returns {texture_key: source} for whichever tab last opened the
        material preview. source can be a file path or a PIL Image — both
        are accepted by the renderer's load_texture()."""
        if getattr(self, '_preview_source', 'material_generator') == 'dual_layer':
            maps = {}
            if 'basecolor' in self._dl_maps:
                maps['albedo'] = self._dl_maps['basecolor']
            if 'normal' in self._dl_maps:
                maps['normal'] = self._dl_maps['normal']
            if 'height' in self._dl_maps:
                maps['height'] = self._dl_maps['height']
            if 'roughness' in self._dl_maps or 'metallic' in self._dl_maps:
                rmaos = self._dl_pack_rmaos_preview()
                if rmaos is not None:
                    maps['rmaos'] = rmaos
            return maps
        # Material Generator — file paths on disk
        MAP = {'diffuse': 'albedo', 'normal': 'normal', 'height': 'height', 'rmaos': 'rmaos'}
        out = {}
        for mat_key, tex_key in MAP.items():
            path = self._mat_paths.get(mat_key, '')
            if path and os.path.isfile(path):
                out[tex_key] = path
        return out

    def _dl_pack_rmaos_preview(self):
        """Dual Layer Builder keeps roughness/metallic as separate in-memory
        images with no packed AO/specular channel — build a throwaway RMAOS
        (R=roughness, G=metalness, B=AO(=white), A=specular(=white)) just for
        the preview, without touching Dual Layer's own save/export flow."""
        rough = self._dl_maps.get('roughness')
        metal = self._dl_maps.get('metallic')
        base = rough or metal
        if base is None:
            return None
        size = base.size
        r = (rough.convert('L').resize(size) if rough else Image.new('L', size, 128))
        g = (metal.convert('L').resize(size) if metal else Image.new('L', size, 0))
        b = Image.new('L', size, 255)  # AO — Dual Layer doesn't compute one
        a = Image.new('L', size, 255)  # specular/smoothness — no source map
        return Image.merge('RGBA', (r, g, b, a))

    def _mat_pack_rmaos(self):
        """Pack generated maps into RMAOS right now using current channel settings."""
        if not _MAT_OK:
            messagebox.showerror('Missing Module', f'material_engine.py failed to load:\n{_MAT_ERR}')
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
                                 f'material_engine.py failed to load:\n{_MAT_ERR}')
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
        settings = {'enabled': {}, 'height': {}, 'normal': {}, 'ao': {},
                    'roughness': {}, 'metalness': {}, 'edge': {}, 'emissive': {},
                    'rmaos': {}}

        for map_name in _me.MAP_ORDER:
            settings['enabled'][map_name] = self._mat_enabled.get(map_name,
                                            tk.BooleanVar(value=True)).get()
            svars = self._mat_svars.get(map_name, {})
            for key, var in svars.items():
                if key.startswith('_'): continue
                settings[map_name][key] = var.get()

        # Inject AI Depth params into height
        try:
            settings['height']['use_ai'] = self._mat_ai_enabled.get()
            settings['height']['ai_model'] = self._mat_ai_model_var.get()
            settings['height']['ai_detail'] = self._mat_ai_detail_var.get()
            settings['height']['ai_strength'] = self._mat_ai_strength_var.get()
        except Exception as e:
            print(f"[AI] Could not inject AI settings: {e}")

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
        """v1.4 - Maps on top (big + responsive), controls on bottom in columns."""
        self._dl_src    = None
        self._dl_maps   = {}
        self._dl_photos = {}
        self._dl_live_enabled = tk.BooleanVar(value=True)
        self._dl_live_job = None
        self._dl_thumb_size = 260
        # AI Depth vars for Dual Layer
        self._dl_ai_enabled = tk.BooleanVar(value=False)
        self._dl_ai_model_var = tk.StringVar(value='depth-anything-small')
        self._dl_ai_detail_var = tk.DoubleVar(value=0.15)
        self._dl_ai_strength_var = tk.DoubleVar(value=1.0)

        # Main vertical split: top = previews (expandable), bottom = controls
        main = tk.Frame(parent, bg=C['bg'])
        main.pack(fill='both', expand=True)

        top = tk.Frame(main, bg=C['bg'])
        top.pack(side='top', fill='both', expand=True, padx=2, pady=2)

        bottom = tk.Frame(main, bg=C['surface'], height=420)
        bottom.pack(side='bottom', fill='x')
        bottom.pack_propagate(False)

        # Bottom controls - scrollable vertically (since many sliders)
        bot_canvas, sf = self._make_scrollable(bottom, C['surface'])

        # Top bar inside bottom for Load + Live toggle + Actions
        top_ctrl = tk.Frame(sf, bg=C['surface'])
        top_ctrl.pack(fill='x', padx=12, pady=8)

        self._dl_src_lbl = tk.Label(top_ctrl, text='No image loaded',
                                     bg=C['surface'], fg=C['text_dim'],
                                     font=('Segoe UI', 9), wraplength=400, anchor='w')
        self._dl_src_lbl.pack(side='left', fill='x', expand=True, padx=(0,10))

        def _pick_src():
            f = filedialog.askopenfilename(
                title='Select diffuse image',
                filetypes=[('Images','*.png *.jpg *.jpeg *.bmp *.tga *.dds'),('All','*.*')])
            if not f: return
            try:
                self._dl_src = Image.open(f).convert('RGB')
                self._dl_src_lbl.config(text=os.path.basename(f), fg=C['text'])
                self._dl_btn_gen.config(state='normal')
                self._dl_update_cell('basecolor', self._dl_src)
                if self._dl_live_enabled.get():
                    self._dl_schedule_live()
            except Exception as e:
                messagebox.showerror('Load Error', str(e))

        self._mkbtn(top_ctrl, 'Load Diffuse', _pick_src, pad=(14,8), font=('Segoe UI', 9, 'bold')).pack(side='left', padx=4)
        ttk.Checkbutton(top_ctrl, text='Live Preview', variable=self._dl_live_enabled, style='Dark.TCheckbutton').pack(side='left', padx=12)

        # AI Depth toggle for Dual Layer
        ai_frame = tk.Frame(sf, bg='#2a2a1a', highlightbackground='#ffcc00', highlightthickness=1)
        ai_frame.pack(fill='x', padx=12, pady=6)
        ai_hdr = tk.Frame(ai_frame, bg='#2a2a1a')
        ai_hdr.pack(fill='x', padx=8, pady=4)
        ttk.Checkbutton(ai_hdr, text='', variable=self._dl_ai_enabled, style='Panel.TCheckbutton').pack(side='left')
        tk.Label(ai_hdr, text='USE AI DEPTH (High Quality like reference)', bg='#2a2a1a', fg='#ffcc00', font=('Segoe UI', 9, 'bold')).pack(side='left', padx=4)
        try:
            _ai_models_dl = _lazy_load_ai().get_available_models() if _lazy_load_ai() and _AI_OK else ['depth-anything-small', 'depth-anything-base', 'marigold-v1']
        except:
            _ai_models_dl = ['depth-anything-small', 'depth-anything-base', 'marigold-v1']
        tk.Label(ai_hdr, text='Model:', bg='#2a2a1a', fg='#ffcc00', font=('Segoe UI', 8)).pack(side='left', padx=(12,2))
        cmb_ai_dl = ttk.Combobox(ai_hdr, textvariable=self._dl_ai_model_var, values=_ai_models_dl, state='readonly', width=18, font=('Segoe UI', 8))
        cmb_ai_dl.pack(side='left', padx=2)
        cmb_ai_dl.bind('<<ComboboxSelected>>', lambda e: self._dl_schedule_live() if self._dl_live_enabled.get() else None)
        # Detail slider
        dframe = tk.Frame(ai_frame, bg='#2a2a1a')
        dframe.pack(fill='x', padx=8, pady=2)
        tk.Label(dframe, text='Detail Blend:', bg='#2a2a1a', fg='#e0d0a0', font=('Segoe UI', 7), width=12, anchor='w').pack(side='left')
        tk.Scale(dframe, from_=0.0, to=0.5, resolution=0.05, orient='horizontal', variable=self._dl_ai_detail_var, bg='#2a2a1a', fg='#e0d0a0', highlightthickness=0, length=120, font=('Segoe UI', 7), command=lambda v: self._dl_schedule_live() if self._dl_live_enabled.get() else None).pack(side='left')
        tk.Label(dframe, text='0=smooth ref, 0.15=keep engravings', bg='#2a2a1a', fg='#a09060', font=('Segoe UI', 7)).pack(side='left', padx=4)

        # Controls columns frame
        cols_frame = tk.Frame(sf, bg=C['surface'])
        cols_frame.pack(fill='x', padx=8, pady=4)
        for i in range(4):
            cols_frame.columnconfigure(i, weight=1, uniform='ctrlcol')

        col_frames = []
        for i in range(4):
            cf = tk.Frame(cols_frame, bg=C['surface'], highlightbackground=C['border'], highlightthickness=0)
            cf.grid(row=0, column=i, sticky='nsew', padx=6, pady=2)
            col_frames.append(cf)

        # Vars
        self._dl_vars = {}
        def _sl(parent_frame, lbl, lo, hi, default, key):
            var = self._mat_compact_slider(parent_frame, lbl, lo, hi, round((hi-lo)/100, 4) if hi-lo>1 else 0.01, default)
            self._dl_vars[key] = var
            def _on_change(*_args):
                if self._dl_live_enabled.get() and self._dl_src is not None:
                    self._dl_schedule_live()
            var.trace_add('write', _on_change)
            return var

        # COL 0 - Base + Normal
        self._section_lbl(col_frames[0], 'BASE COLOR')
        _sl(col_frames[0], 'BC Bright',  -0.5, 0.5, 0.0, 'base_brightness')
        _sl(col_frames[0], 'BC Contrast', 0.5, 2.0, 1.0, 'base_contrast')
        _sl(col_frames[0], 'BC Saturat',  0.0, 2.0, 1.0, 'base_saturation')
        self._sep(col_frames[0])
        self._section_lbl(col_frames[0], 'NORMAL MAP')
        _sl(col_frames[0], 'Norm Str', 0.1, 10.0, 3.0, 'normal_strength')
        _sl(col_frames[0], 'Norm Blur', 0.0, 10.0, 0.0, 'normal_blur')
        _sl(col_frames[0], 'Norm Z', 0.1, 5.0, 1.0, 'normal_z')
        _sl(col_frames[0], 'Norm Det', 0.0, 1.0, 0.2, 'normal_detail')
        self._dl_flip_x = tk.BooleanVar(value=False)
        self._dl_flip_y = tk.BooleanVar(value=False)
        self._dl_normal_mode = tk.StringVar(value='Sobel')
        fr = tk.Frame(col_frames[0], bg=C['surface']); fr.pack(fill='x', pady=2)
        cbx = ttk.Checkbutton(fr, text='Flip X', variable=self._dl_flip_x, style='Dark.TCheckbutton')
        cbx.pack(side='left'); cbx.config(command=lambda: self._dl_schedule_live() if self._dl_live_enabled.get() else None)
        cby = ttk.Checkbutton(fr, text='Flip Y', variable=self._dl_flip_y, style='Dark.TCheckbutton')
        cby.pack(side='left', padx=(8,0)); cby.config(command=lambda: self._dl_schedule_live() if self._dl_live_enabled.get() else None)
        fr2 = tk.Frame(col_frames[0], bg=C['surface']); fr2.pack(fill='x', pady=2)
        tk.Label(fr2, text='Mode:', bg=C['surface'], fg=C['text'], font=('Segoe UI', 8)).pack(side='left')
        cmb_nm = ttk.Combobox(fr2, textvariable=self._dl_normal_mode, values=['Sobel','Scharr'], width=8, state='readonly')
        cmb_nm.pack(side='left', padx=4); cmb_nm.bind('<<ComboboxSelected>>', lambda e: self._dl_schedule_live() if self._dl_live_enabled.get() else None)

        # COL 1 - Roughness + Metallic
        self._section_lbl(col_frames[1], 'ROUGHNESS')
        _sl(col_frames[1], 'Rough Contr', 0.5, 3.0, 1.0, 'rough_contrast')
        _sl(col_frames[1], 'Rough Min', 0.0, 1.0, 0.0, 'rough_min')
        _sl(col_frames[1], 'Rough Max', 0.0, 1.0, 1.0, 'rough_max')
        _sl(col_frames[1], 'Rough Gamma', 0.1, 3.0, 1.0, 'rough_gamma')
        _sl(col_frames[1], 'Rough Blur', 0.0, 10.0, 0.0, 'rough_blur')
        _sl(col_frames[1], 'Rough AO', 0.0, 1.0, 0.0, 'rough_ao_mix')
        self._dl_rough_inv = tk.BooleanVar(value=True)
        rcb = ttk.Checkbutton(col_frames[1], text='Invert Roughness', variable=self._dl_rough_inv, style='Dark.TCheckbutton')
        rcb.pack(anchor='w', pady=2); rcb.config(command=lambda: self._dl_schedule_live() if self._dl_live_enabled.get() else None)
        self._sep(col_frames[1])
        self._section_lbl(col_frames[1], 'METALLIC - UPGRADED')
        self._dl_metal_mode = tk.StringVar(value='Flat')
        fr3 = tk.Frame(col_frames[1], bg=C['surface']); fr3.pack(fill='x', pady=2)
        tk.Label(fr3, text='Mode:', bg=C['surface'], fg=C['text'], font=('Segoe UI', 8)).pack(side='left')
        cmb_mm = ttk.Combobox(fr3, textvariable=self._dl_metal_mode, values=['Flat','Threshold','Color Detect'], width=12, state='readonly')
        cmb_mm.pack(side='left', padx=4); cmb_mm.bind('<<ComboboxSelected>>', lambda e: self._dl_schedule_live() if self._dl_live_enabled.get() else None)
        _sl(col_frames[1], 'Metal Level', 0.0, 1.0, 0.0, 'metallic')
        _sl(col_frames[1], 'Metal Thresh', 0.0, 1.0, 0.6, 'metal_thresh')
        _sl(col_frames[1], 'Metal Toler', 0.0, 0.5, 0.1, 'metal_toler')
        _sl(col_frames[1], 'Metal Contr', 0.5, 3.0, 1.0, 'metal_contrast')
        _sl(col_frames[1], 'Metal Blur', 0.0, 10.0, 0.0, 'metal_blur')

        # COL 2 - Height / AO
        self._section_lbl(col_frames[2], 'HEIGHT / PARALLAX')
        self._dl_height_src = tk.StringVar(value='Luminance')
        fr4 = tk.Frame(col_frames[2], bg=C['surface']); fr4.pack(fill='x', pady=2)
        tk.Label(fr4, text='Source:', bg=C['surface'], fg=C['text'], font=('Segoe UI', 8)).pack(side='left')
        cmb_hs = ttk.Combobox(fr4, textvariable=self._dl_height_src, values=['Luminance','Red Channel','Average'], width=12, state='readonly')
        cmb_hs.pack(side='left', padx=4); cmb_hs.bind('<<ComboboxSelected>>', lambda e: self._dl_schedule_live() if self._dl_live_enabled.get() else None)
        _sl(col_frames[2], 'Height Contr', 0.5, 3.0, 1.0, 'height_contrast')
        _sl(col_frames[2], 'Height Bright', -1.0, 1.0, 0.0, 'height_brightness')
        _sl(col_frames[2], 'Height Blur', 0.0, 10.0, 0.0, 'height_blur')
        _sl(col_frames[2], 'Height Mid', 0.0, 1.0, 0.5, 'height_midlevel')
        _sl(col_frames[2], 'Parallax', 0.0, 0.1, 0.05, 'parallax')
        self._dl_height_inv = tk.BooleanVar(value=False)
        hcb = ttk.Checkbutton(col_frames[2], text='Invert Height', variable=self._dl_height_inv, style='Dark.TCheckbutton')
        hcb.pack(anchor='w', pady=2); hcb.config(command=lambda: self._dl_schedule_live() if self._dl_live_enabled.get() else None)
        self._sep(col_frames[2])
        self._section_lbl(col_frames[2], 'AO / CURVATURE')
        _sl(col_frames[2], 'AO Strength', 0.0, 2.0, 1.0, 'ao_strength')
        _sl(col_frames[2], 'AO Radius', 0.0, 10.0, 3.0, 'ao_radius')

        # COL 3 - Clear Coat + Actions
        self._section_lbl(col_frames[3], 'CLEAR COAT')
        _sl(col_frames[3], 'Coat Opacity', 0.0, 1.0, 0.0, 'coat_opacity')
        _sl(col_frames[3], 'Coat Rough', 0.0, 1.0, 0.2, 'coat_roughness')
        _sl(col_frames[3], 'Coat Norm', 0.0, 2.0, 1.0, 'coat_normal_str')
        _sl(col_frames[3], 'Coat Metal', 0.0, 1.0, 0.0, 'coat_metallic')
        _sl(col_frames[3], 'Coat Parallax', 0.0, 0.1, 0.02, 'coat_parallax')
        self._dl_coat_color = '#ffffff'
        cc_row = tk.Frame(col_frames[3], bg=C['surface']); cc_row.pack(fill='x', pady=4)
        tk.Label(cc_row, text='Coat Color', bg=C['surface'], fg=C['text'], font=('Segoe UI', 8), width=10, anchor='w').pack(side='left')
        self._dl_cc_btn = RoundedButton(cc_row, text='#ffffff', bg='#ffffff', fg='#111111', font=('Segoe UI', 8), pad=(10, 5), command=self._dl_pick_coat_color)
        self._dl_cc_btn.pack(side='left', padx=4)
        self._dl_use_coat_normal = tk.BooleanVar(value=False)
        ucc = ttk.Checkbutton(col_frames[3], text='Use blurred normal as coat normal', variable=self._dl_use_coat_normal, style='Dark.TCheckbutton')
        ucc.pack(anchor='w', pady=2); ucc.config(command=lambda: self._dl_schedule_live() if self._dl_live_enabled.get() else None)
        self._sep(col_frames[3])
        self._dl_btn_gen = self._mkbtn(col_frames[3], 'Generate All 8 Maps', self._dl_generate, pad=(12,10), font=('Segoe UI', 10, 'bold'))
        self._dl_btn_gen.pack(fill='x', pady=4); self._dl_btn_gen.config(state='disabled')
        self._dl_prog = tk.DoubleVar(value=0)
        ttk.Progressbar(col_frames[3], variable=self._dl_prog, maximum=100).pack(fill='x', pady=4)
        self._mkbtn(col_frames[3], 'Save All Maps', self._dl_save_all, bg=C['panel2'], fg=C['text'], pad=(12,8)).pack(fill='x', pady=4)
        self._mkbtn(col_frames[3], 'Open Material Preview', lambda: self._open_material_preview(source='dual_layer'), bg='#1a3a2a', fg=C['success'], pad=(12, 8), font=('Segoe UI', 9, 'bold')).pack(fill='x', pady=6)

        # --- TOP PREVIEWS - BIG + RESPONSIVE ---
        # Canvas for previews that resizes
        rc = tk.Canvas(top, bg=C['bg'], highlightthickness=0)
        rsb = ttk.Scrollbar(top, orient='vertical', command=rc.yview)
        gf = tk.Frame(rc, bg=C['bg'])
        gf.bind('<Configure>', lambda e: rc.configure(scrollregion=rc.bbox('all')))
        gw = rc.create_window((0,0), window=gf, anchor='nw')
        rc.configure(yscrollcommand=rsb.set)
        def _on_rc_resize(e):
            rc.itemconfig(gw, width=e.width)
            # Responsive thumb calc: 4 cols, with padding
            avail = max(800, e.width - 40)
            new_thumb = max(200, min(450, (avail // 4) - 24))
            if abs(new_thumb - self._dl_thumb_size) > 15:
                self._dl_thumb_size = new_thumb
                for info in getattr(self, '_dl_cells', {}).values():
                    info['canvas'].config(width=new_thumb, height=new_thumb)
                    info['size'] = new_thumb
                # Re-render existing maps at new size
                for k, im in self._dl_maps.items():
                    if k in self._dl_cells:
                        self._dl_update_cell(k, im)
        rc.bind('<Configure>', _on_rc_resize)
        rc.pack(side='left', fill='both', expand=True)
        rsb.pack(side='right', fill='y')
        self._bind_wheel_scroll(rc, gf)
        for c in range(4):
            gf.columnconfigure(c, weight=1, uniform='dlc')
        self._dl_cells = {}
        CELLS = [('basecolor','BASE COLOR',C['accent']),('normal','NORMAL','#569cd6'),('roughness','ROUGHNESS','#ce9178'),('metallic','METALLIC','#dcdcaa'),('height','HEIGHT','#4ec9b0'),('coatcolor','COAT COLOR','#c586c0'),('coatnormal','COAT NORMAL','#9cdcfe'),('coatroughness','COAT ROUGHNESS','#ff8c00'),]
        for idx, (key, disp, col) in enumerate(CELLS):
            r = idx // 4
            c = idx % 4
            cell = tk.Frame(gf, bg=C['panel'], highlightbackground=C['border'], highlightthickness=1)
            cell.grid(row=r, column=c, padx=8, pady=8, sticky='nsew')
            gf.rowconfigure(r, weight=1)
            tk.Label(cell, text=disp, bg=C['panel'], fg=col, font=('Segoe UI', 10, 'bold')).pack(pady=(8,4))
            THUMB = self._dl_thumb_size
            cv = tk.Canvas(cell, width=THUMB, height=THUMB, bg='#111', highlightthickness=0, cursor='hand2')
            cv.pack(padx=8, pady=4, expand=True, fill='both')
            cv.create_text(THUMB//2, THUMB//2, text='No image', fill=C['text_dim'], font=('Segoe UI', 9), tags='ph')
            cv.bind('<Button-1>', lambda e, k=key: self._dl_open_preview(k))
            tk.Label(cell, text='Click to preview', bg=C['panel'], fg=C['text_dim'], font=('Segoe UI', 8)).pack(pady=(0,8))
            self._dl_cells[key] = {'canvas': cv, 'size': THUMB, 'frame': cell}

    def _dl_schedule_live(self):
        if getattr(self, '_dl_live_job', None) is not None:
            try:
                self.root.after_cancel(self._dl_live_job)
            except:
                pass
        self._dl_live_job = self.root.after(150, self._dl_generate)

    def _dl_pick_coat_color(self):
        c = colorchooser.askcolor(self._dl_coat_color, title='Coat Color')
        if c[1]:
            self._dl_coat_color = c[1]
            self._dl_cc_btn.config(text=c[1], bg=c[1])
            if hasattr(self, '_dl_live_enabled') and self._dl_live_enabled.get() and self._dl_src is not None:
                self._dl_schedule_live()

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
            img = self._dl_src
            w, h = img.size
            arr = np.array(img).astype(np.float32) / 255.0
            lum = (0.299*arr[:,:,0] + 0.587*arr[:,:,1] + 0.114*arr[:,:,2]).astype(np.float32)
            import cv2 as _cv2

            def getv(k, d): 
                try: return self._dl_vars[k].get()
                except: return d

            steps = 8
            def prog(i): self.root.after(0, lambda: self._dl_prog.set(i/steps*100))

            # 1 BASE COLOR with Bright/Contrast/Sat
            b_b = getv('base_brightness', 0.0)
            b_c = getv('base_contrast', 1.0)
            b_s = getv('base_saturation', 1.0)
            base_arr = (arr - 0.5) * b_c + 0.5 + b_b
            gray3 = lum[:,:,None]
            base_arr = gray3 + (base_arr - gray3) * b_s
            base_arr = np.clip(base_arr, 0, 1)
            base_img = Image.fromarray((base_arr * 255).astype(np.uint8))
            self._dl_maps['basecolor'] = base_img
            self.root.after(0, lambda: self._dl_update_cell('basecolor', base_img))
            prog(1)

            # 2 NORMAL with blur, Z, flip, detail, Sobel/Scharr
            n_str = getv('normal_strength', 3.0)
            n_blur = getv('normal_blur', 0.0)
            n_z = getv('normal_z', 1.0)
            n_det = getv('normal_detail', 0.2)
            lum_n = lum.copy()
            if n_blur > 0.01:
                lum_n = _cv2.GaussianBlur(lum_n, (0,0), n_blur)
            mode = self._dl_normal_mode.get() if hasattr(self, '_dl_normal_mode') else 'Sobel'
            if mode == 'Scharr':
                dx = _cv2.Scharr(lum_n, _cv2.CV_32F, 1, 0) * n_str * 0.15
                dy = _cv2.Scharr(lum_n, _cv2.CV_32F, 0, 1) * n_str * 0.15
            else:
                dx = _cv2.Sobel(lum_n, _cv2.CV_32F, 1, 0, ksize=3) * n_str
                dy = _cv2.Sobel(lum_n, _cv2.CV_32F, 0, 1, ksize=3) * n_str
            if hasattr(self, '_dl_flip_x') and self._dl_flip_x.get(): dx = -dx
            if hasattr(self, '_dl_flip_y') and self._dl_flip_y.get(): dy = -dy
            dz = np.ones_like(lum_n) * max(n_z, 0.01)
            length = np.sqrt(dx*dx + dy*dy + dz*dz)
            nx = dx/length*0.5+0.5
            ny = dy/length*0.5+0.5
            nz = dz/length*0.5+0.5
            if n_det > 0.001:
                hp = lum - _cv2.GaussianBlur(lum, (0,0), 2.0)
                nx = np.clip(nx + hp * n_det * 0.5, 0, 1)
                ny = np.clip(ny + hp * n_det * 0.5, 0, 1)
            norm_arr = np.stack([nx, ny, nz], axis=-1) * 255.0
            norm_img = Image.fromarray(norm_arr.astype(np.uint8))
            self._dl_maps['normal'] = norm_img
            self.root.after(0, lambda: self._dl_update_cell('normal', norm_img))
            prog(2)

            # 3 ROUGHNESS with min/max/gamma/blur/AO mix
            r_con = getv('rough_contrast', 1.0)
            r_min = getv('rough_min', 0.0)
            r_max = getv('rough_max', 1.0)
            r_gam = getv('rough_gamma', 1.0)
            r_blur = getv('rough_blur', 0.0)
            r_ao = getv('rough_ao_mix', 0.0)
            rough = lum.copy()
            if hasattr(self, '_dl_rough_inv') and self._dl_rough_inv.get(): rough = 1.0 - rough
            rough = r_min + rough * (r_max - r_min)
            rough = np.power(np.clip(rough, 0.001, 0.999), max(r_gam,0.01))
            rough = np.clip((rough - 0.5) * r_con + 0.5, 0, 1)
            if r_blur > 0.01:
                rough = _cv2.GaussianBlur(rough, (0,0), r_blur)
            # AO mix will be applied after height calc, store for now
            self._dl_temp_rough = rough
            rough_img = Image.fromarray((rough*255).astype(np.uint8))
            self._dl_maps['roughness'] = rough_img
            self.root.after(0, lambda: self._dl_update_cell('roughness', rough_img))
            prog(3)

            # 4 METALLIC with modes
            m_mode = self._dl_metal_mode.get() if hasattr(self, '_dl_metal_mode') else 'Flat'
            m_level = getv('metallic', 0.0)
            m_thr = getv('metal_thresh', 0.6)
            m_tol = getv('metal_toler', 0.1)
            m_con = getv('metal_contrast', 1.0)
            m_blur = getv('metal_blur', 0.0)
            if m_mode == 'Flat':
                metal = np.ones_like(lum) * m_level
            elif m_mode == 'Threshold':
                # smoothstep threshold
                lo = max(m_thr - m_tol, 0.0)
                hi = min(m_thr + m_tol, 1.0)
                denom = max(hi-lo, 0.001)
                metal = np.clip((lum - lo) / denom, 0, 1)
            else: # Color Detect
                maxc = arr.max(axis=2)
                minc = arr.min(axis=2)
                sat = maxc - minc
                # low sat + bright = metal
                metal = np.where((sat < 0.15) & (lum > m_thr), 1.0, 0.0).astype(np.float32)
                # feather with tolerance
                metal = _cv2.GaussianBlur(metal, (0,0), m_tol*5+0.5) if m_tol>0 else metal
            metal = np.clip((metal - 0.5) * m_con + 0.5, 0, 1)
            if m_blur > 0.01:
                metal = _cv2.GaussianBlur(metal, (0,0), m_blur)
            metal_img = Image.new('L', (w,h), 0)
            metal_img = Image.fromarray((metal*255).astype(np.uint8))
            self._dl_maps['metallic'] = metal_img
            self.root.after(0, lambda: self._dl_update_cell('metallic', metal_img))
            prog(4)

            # 5 HEIGHT with AI Depth support (reference quality)
            # Check if AI Depth enabled
            _lazy_load_ai()
            use_ai_dl = False
            try:
                use_ai_dl = self._dl_ai_enabled.get() and _AI_OK and _ai is not None
            except:
                use_ai_dl = False
            
            if use_ai_dl:
                try:
                    # Use AI depth engine for reference-like depth
                    pil_src = Image.fromarray((arr * 255).astype(np.uint8))
                    model_type = self._dl_ai_model_var.get() if hasattr(self, '_dl_ai_model_var') else 'depth-anything-v2-large'
                    detail_blend = self._dl_ai_detail_var.get() if hasattr(self, '_dl_ai_detail_var') else 0.35
                    strength_ai = self._dl_ai_strength_var.get() if hasattr(self, '_dl_ai_strength_var') else 1.0
                    _la = _lazy_load_ai()
                    depth_pil = None
                    if _la is not None:
                        depth_pil = _la.estimate_depth_pil(pil_src, model_type=model_type, detail_blend=detail_blend, use_guided=True, high_res=True)
                    if depth_pil is None:
                        raise RuntimeError("AI depth returned None")
                    ht = np.array(depth_pil).astype(np.float32) / 255.0
                    # Apply contrast/brightness/midlevel on top of AI depth
                    h_con = getv('height_contrast', 1.0)
                    h_bri = getv('height_brightness', 0.0)
                    h_blur = getv('height_blur', 0.0)
                    h_mid = getv('height_midlevel', 0.5)
                    ht = np.clip((ht - 0.5) * h_con * strength_ai + 0.5 + h_bri, 0, 1)
                    ht = np.clip(ht - (h_mid - 0.5), 0, 1)
                    if h_blur > 0.01:
                        ht = _cv2.GaussianBlur(ht, (0,0), h_blur)
                except Exception as e:
                    print(f"[DL] AI depth failed: {e}, fallback to luminance")
                    # Fallback to luminance path below
                    h_src_mode = self._dl_height_src.get() if hasattr(self, '_dl_height_src') else 'Luminance'
                    if h_src_mode == 'Red Channel': ht = arr[:,:,0]
                    elif h_src_mode == 'Average': ht = arr.mean(axis=2)
                    else: ht = lum.copy()
                    if hasattr(self, '_dl_height_inv') and self._dl_height_inv.get(): ht = 1.0 - ht
                    h_con = getv('height_contrast', 1.0)
                    h_bri = getv('height_brightness', 0.0)
                    h_blur = getv('height_blur', 0.0)
                    h_mid = getv('height_midlevel', 0.5)
                    ht = np.clip((ht - 0.5) * h_con + 0.5 + h_bri, 0, 1)
                    ht = np.clip(ht - (h_mid - 0.5), 0, 1)
                    if h_blur > 0.01:
                        ht = _cv2.GaussianBlur(ht, (0,0), h_blur)
            else:
                h_src_mode = self._dl_height_src.get() if hasattr(self, '_dl_height_src') else 'Luminance'
                if h_src_mode == 'Red Channel': ht = arr[:,:,0]
                elif h_src_mode == 'Average': ht = arr.mean(axis=2)
                else: ht = lum.copy()
                if hasattr(self, '_dl_height_inv') and self._dl_height_inv.get(): ht = 1.0 - ht
                h_con = getv('height_contrast', 1.0)
                h_bri = getv('height_brightness', 0.0)
                h_blur = getv('height_blur', 0.0)
                h_mid = getv('height_midlevel', 0.5)
                ht = np.clip((ht - 0.5) * h_con + 0.5 + h_bri, 0, 1)
                ht = np.clip(ht - (h_mid - 0.5), 0, 1)
                if h_blur > 0.01:
                    ht = _cv2.GaussianBlur(ht, (0,0), h_blur)
            height_img = Image.fromarray((ht*255).astype(np.uint8))
            self._dl_maps['height'] = height_img
            self.root.after(0, lambda: self._dl_update_cell('height', height_img))
            prog(5)

            # Apply AO mix to roughness now that we have height
            ao_str = getv('ao_strength', 1.0)
            ao_rad = getv('ao_radius', 3.0)
            # simple AO = inverted blurred height
            ao = 1.0 - _cv2.GaussianBlur(1.0 - ht, (0,0), max(ao_rad,0.1)) * ao_str
            ao = np.clip(ao, 0, 1)
            if r_ao > 0.001:
                rough_final = self._dl_temp_rough * (1.0 - r_ao) + (1.0 - ao) * r_ao
                rough_final = np.clip(rough_final, 0, 1)
                rough_img2 = Image.fromarray((rough_final*255).astype(np.uint8))
                self._dl_maps['roughness'] = rough_img2
                self.root.after(0, lambda: self._dl_update_cell('roughness', rough_img2))

            # 6 COAT COLOR
            hex_c = self._dl_coat_color
            rgb = tuple(int(hex_c[i:i+2],16) for i in (1,3,5))
            op = getv('coat_opacity', 0.0)
            coat = Image.new('RGB', (w,h), rgb)
            if op < 0.999:
                coat = Image.blend(Image.new('RGB',(w,h),(0,0,0)), coat, op)
            self._dl_maps['coatcolor'] = coat
            self.root.after(0, lambda: self._dl_update_cell('coatcolor', coat))
            prog(6)

            # 7 COAT NORMAL
            cn_str = getv('coat_normal_str', 1.0)
            if hasattr(self, '_dl_use_coat_normal') and self._dl_use_coat_normal.get():
                from PIL import ImageFilter
                blur_rad = max(2.0 * cn_str, 0.5)
                cn = norm_img.filter(ImageFilter.GaussianBlur(blur_rad))
            else:
                cn = Image.new('RGB', (w,h), (128,128,255))
            self._dl_maps['coatnormal'] = cn
            self.root.after(0, lambda: self._dl_update_cell('coatnormal', cn))
            prog(7)

            # 8 COAT ROUGHNESS
            cr = int(getv('coat_roughness', 0.2) * 255)
            cr_img = Image.new('L', (w,h), cr)
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

        self._dl_scroll_canvas, sf = self._make_scrollable(left, C['surface'])

        self._section_lbl(sf, '_p MAP BUILDER',
            'Batch-generates _n (normal map) and _p (height map) PNG files for every '
            'diffuse PNG in the input folder. Skips files that already have known '
            'map suffixes.')

        # Folders
        self._pm_in_var  = tk.StringVar(value=self.input_dir.get())
        self._pm_out_var = tk.StringVar(value=self.output_dir.get())
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
                     variable=var, bg=C['accent'], fg=C['text'],
                     troughcolor=C['input'], highlightthickness=0, activebackground=C['accent_hi'],
                     showvalue=False, sliderlength=14, sliderrelief='raised', bd=1,
                     command=lambda v, l=val_lbl: l.config(text=f'{float(v):.3f}')
                     ).pack(fill='x', expand=True, padx=(0,4))
            self._pm_vars[key] = var

        _sl('Normal Strength',   0.5, 20.0, 0.1,  6.0,  'strength')
        _sl('Blur Radius',       0.0,  8.0, 0.25, 2.5,  'blur_radius')
        _sl('Gradient Mult.',    0.05, 1.0, 0.05, 0.25, 'grad_mult')

        # AI Depth for batch
        self._pm_ai_enabled = tk.BooleanVar(value=False)
        self._pm_ai_model_var = tk.StringVar(value='depth-anything-small')
        self._pm_ai_detail_var = tk.DoubleVar(value=0.15)
        ai_f = tk.Frame(sf, bg='#2a2a1a', highlightbackground='#ffcc00', highlightthickness=1)
        ai_f.pack(fill='x', padx=16, pady=8)
        ttk.Checkbutton(ai_f, text='USE AI DEPTH for Height (reference quality)', variable=self._pm_ai_enabled, style='Dark.TCheckbutton').pack(anchor='w', padx=8, pady=4)
        mr = tk.Frame(ai_f, bg='#2a2a1a'); mr.pack(fill='x', padx=8, pady=2)
        tk.Label(mr, text='Model:', bg='#2a2a1a', fg='#ffcc00', font=('Segoe UI', 8)).pack(side='left')
        try:
            _ai_models_pm = _lazy_load_ai().get_available_models() if _lazy_load_ai() and _AI_OK else ['depth-anything-small', 'depth-anything-base', 'marigold-v1']
        except:
            _ai_models_pm = ['depth-anything-small', 'depth-anything-base', 'marigold-v1']
        ttk.Combobox(mr, textvariable=self._pm_ai_model_var, values=_ai_models_pm, state='readonly', width=20).pack(side='left', padx=4)
        dr = tk.Frame(ai_f, bg='#2a2a1a'); dr.pack(fill='x', padx=8, pady=2)
        tk.Label(dr, text='Detail Blend: 0=smooth ref, 0.15=keep engravings', bg='#2a2a1a', fg='#e0d0a0', font=('Segoe UI', 7)).pack(anchor='w')
        tk.Scale(dr, from_=0.0, to=0.5, resolution=0.05, orient='horizontal', variable=self._pm_ai_detail_var, bg='#2a2a1a', length=200).pack(fill='x')

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
        for fmt in ['PNG','TGA','BMP','DDS']:
            ttk.Radiobutton(fmt_row, text=fmt, variable=self._pm_fmt_var, value=fmt,
                            style='Dark.TRadiobutton').pack(side='left', padx=(0,12))
        tk.Label(sf, text='DDS output: _n uses BC7_UNORM, _p uses BC4_UNORM '
                          '(same defaults as DDS Tool/Resize Tool\'s rules)',
                 bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 7)).pack(anchor='w', padx=16)

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
        use_ai_pm  = self._pm_ai_enabled.get() if hasattr(self, '_pm_ai_enabled') else False
        ai_model_pm = self._pm_ai_model_var.get() if hasattr(self, '_pm_ai_model_var') else 'depth-anything-small'
        ai_detail_pm = self._pm_ai_detail_var.get() if hasattr(self, '_pm_ai_detail_var') else 0.15
        skip_raw   = self._pm_skip_var.get()
        skip_sfx   = tuple(s.strip().lower() for s in skip_raw.split(',') if s.strip())
        fmt        = self._pm_fmt_var.get().lower()

        if fmt == 'dds' and not self.texconv_ok:
            messagebox.showerror('texconv required', 'DDS output requires texconv.exe.')
            return

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

                    # Height field - with AI Depth support
                    if use_ai_pm and '_AI_OK' in globals() and _AI_OK and _ai is not None:
                        try:
                            _la = _lazy_load_ai()
                            depth_pil = None
                            if depth_pil: depth_pil = _la.estimate_depth_pil(img, model_type=ai_model_pm, detail_blend=ai_detail_pm)
                            height = np.array(depth_pil).astype(np.float32) / 255.0
                            if blur_r > 0:
                                height_pil = Image.fromarray((height*255).astype(np.uint8))
                                height_pil = height_pil.filter(_IF.GaussianBlur(radius=blur_r))
                                height = np.array(height_pil).astype(np.float32) / 255.0
                        except Exception as e:
                            print(f"[PM] AI depth failed: {e}, fallback")
                            gray = img.convert('L')
                            if blur_r > 0:
                                gray = gray.filter(_IF.GaussianBlur(radius=blur_r))
                            height = np.array(gray).astype(np.float32) / 255.0
                    else:
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

                    if fmt == 'dds':
                        # Save PNG first (texconv needs a real file to read),
                        # then compress each to its own format - same pattern
                        # Normal Map Generator uses, and the same default
                        # formats DDS Tool/Resize Tool assign to _n/_p.
                        n_png = os.path.join(out_d, f'{base}_n.png')
                        p_png = os.path.join(out_d, f'{base}_p.png')
                        normal_img.save(n_png)
                        height_img.save(p_png)

                        n_dds = os.path.join(out_d, f'{base}_n.dds')
                        p_dds = os.path.join(out_d, f'{base}_p.dds')
                        n_ok = _tc_compress(n_png, n_dds, 'BC7_UNORM')
                        p_ok = _tc_compress(p_png, p_dds, 'BC4_UNORM')

                        if n_ok: os.remove(n_png)
                        else: self._log(self.pm_log, '  ⚠ _n DDS compress failed, kept PNG', C['warn'])
                        if p_ok: os.remove(p_png)
                        else: self._log(self.pm_log, '  ⚠ _p DDS compress failed, kept PNG', C['warn'])

                        n_name = f'{base}_n.dds' if n_ok else f'{base}_n.png'
                        p_name = f'{base}_p.dds' if p_ok else f'{base}_p.png'
                    else:
                        normal_img.save(os.path.join(out_d, f'{base}_n.{fmt}'))
                        height_img.save(os.path.join(out_d, f'{base}_p.{fmt}'))
                        n_name = f'{base}_n.{fmt}'
                        p_name = f'{base}_p.{fmt}'

                    self._log(self.pm_log,
                              f'  ✓ {n_name}  +  {p_name}', C['success'])
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
        icon = resource_path('TG_ICO.ico')
        if os.path.exists(icon): root.iconbitmap(icon)
    except Exception: pass
    TextureGeneratorApp(root)
    root.mainloop()

if __name__ == '__main__':
    main()


    # =========================================================================


    # =========================================================================
