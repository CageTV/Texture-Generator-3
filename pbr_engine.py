"""
PBR Engine  –  In-process implementation of all Skyking PBR toolkit operations.

Covers:
  build_pbr          – Build PBR DDS textures from loose PBR maps
  generate_parallax  – Generate Complex Parallax and/or PBR textures from diffuse+normal
  generate_json      – Build PBRNIFPatcher JSON config from a mod folder
  convert_to_pbr     – Convert Complex Parallax sets → Community Shaders PBR
  convert_to_complex – Convert PBR sets → Complex Parallax _m format
"""

import copy
import json
import shutil
import subprocess
import sys
import os
from pathlib import Path
from typing import Callable, Optional, Dict

import cv2
import numpy as np

import platform_tools as _pt


# ─── Runtime helpers ──────────────────────────────────────────────────────────

def get_texconv() -> Path:
    """Kept for backwards compatibility with any code that wants the raw
    Windows exe path. Prefer texconv_available()/dds_to_png_via_texconv()/
    png_to_dds(), which work cross-platform via platform_tools."""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / 'texconv.exe'
    return Path(os.path.dirname(os.path.abspath(__file__))) / 'texconv.exe'


def texconv_available() -> bool:
    return _pt.texconv_available()


# ─── Supported formats ────────────────────────────────────────────────────────

SUPPORTED_EXTS = {'.dds', '.png', '.tga', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'}

# ─── Image I/O ────────────────────────────────────────────────────────────────

def write_png(path, img) -> Path:
    cv2.imwrite(str(path), img)
    return Path(path)


def dds_to_png_via_texconv(dds_path, tmp_dir) -> Optional[Path]:
    out = _pt.dds_decode(dds_path, tmp_dir)
    return Path(out) if out else None


def read_image(path, tmp, flags=None):
    """Read image, handling DDS by converting to PNG via texconv first."""
    path = Path(path)
    if flags is None:
        flags = cv2.IMREAD_UNCHANGED
    if path.suffix.lower() == '.dds':
        png = dds_to_png_via_texconv(path, tmp)
        if png and png.exists():
            return cv2.imread(str(png), flags), png
        return None, None
    img = cv2.imread(str(path), flags)
    return img, path


def read_gray(path, tmp):
    return read_image(path, tmp, cv2.IMREAD_GRAYSCALE)


def image_to_png(src_path, tmp) -> Optional[Path]:
    """Convert any supported image to PNG in tmp dir."""
    src = Path(src_path)
    if src.suffix.lower() == '.dds':
        return dds_to_png_via_texconv(src, tmp)
    img = cv2.imread(str(src), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    out = Path(tmp) / (src.stem + '.png')
    cv2.imwrite(str(out), img)
    return out


def png_to_dds(png_path, out_path, fmt='BC7_UNORM', srgb=False) -> bool:
    """Convert PNG → DDS. `srgb` is folded into the fmt string for the
    Windows/texconv path (e.g. 'BC7_UNORM_SRGB'); nvcompress on Linux has no
    separate srgb flag so it's compressed the same either way."""
    dxgi_fmt = fmt if (not srgb or fmt.endswith('_SRGB')) else fmt + '_SRGB'
    return _pt.dds_encode(png_path, out_path, dxgi_fmt)


def copy_or_convert_to_dds(src_path, dst_path, fmt, preserve_alpha, tmp) -> bool:
    src, dst = Path(src_path), Path(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == '.dds':
        shutil.copy2(str(src), str(dst))
        return True
    png = image_to_png(src, tmp)
    if png and png.exists():
        return png_to_dds(png, dst, fmt, srgb=False)
    return False


# ─── Texture set scanning ─────────────────────────────────────────────────────

# Ordered longest-first so _rmaos is matched before bare suffixes
_SUFFIX_MAP = [
    ('_rmaos', 'rmaos'),
    ('_n',     'normal'),
    ('_m',     'complex_m'),
    ('_p',     'height'),
    ('_d',     'albedo'),
]


def identify_channel(stem: str):
    """Return (base_stem, channel_key) for a texture filename stem."""
    lower = stem.lower()
    for suffix, channel in _SUFFIX_MAP:
        if lower.endswith(suffix):
            return stem[: -len(suffix)], channel
    return stem, 'albedo'


def scan_texture_sets(src_dir, supported_exts=None) -> dict:
    """
    Recursively scan src_dir and group files into texture sets.
    Returns {set_key: {channel: Path, ...}, ...}
    First file found per channel wins (no overwrite).
    """
    if supported_exts is None:
        supported_exts = SUPPORTED_EXTS
    src = Path(src_dir)
    sets: Dict[str, dict] = {}
    for path in src.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in supported_exts:
            continue
        base, channel = identify_channel(path.stem)
        rel_dir = path.parent.relative_to(src)
        key = str(rel_dir / base).lower().replace('/', '\\')
        bucket = sets.setdefault(key, {})
        if channel not in bucket:          # first wins
            bucket[channel] = path
    return sets


# ─── Config helpers ───────────────────────────────────────────────────────────

def deep_update(target: dict, source: dict):
    for k, v in source.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            deep_update(target[k], v)
        else:
            target[k] = v


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 1 – PBR BUILDER  (build_pbr.py)
# Converts loose PBR maps → packed DDS textures for Community Shaders.
# ═════════════════════════════════════════════════════════════════════════════

_BUILD_PBR_ALIASES_DEFAULT = {
    'albedo':    ['albedo','basecolor','base_color','diffuse','diff','color','col','d'],
    'normal':    ['normal','normalgl','normaldx','norm','n'],
    'roughness': ['roughness','rough','r'],
    'metalness': ['metallic','metalness','metal','m'],
    'ao':        ['ao','ambientocclusion','occlusion'],
    'height':    ['height','displacement','disp','parallax','bump'],
    'specular':  ['specular','spec','s'],
}


def _norm_alias(s: str) -> str:
    return s.lower().replace('_','').replace('-','').replace(' ','')


def _group_by_base(files):
    out = {}
    for f in files:
        base = f.stem.split('_')[0]
        out.setdefault(base, []).append(f)
    return out


def _match_aliases(files, aliases):
    res = {}
    for f in files:
        n = _norm_alias(f.stem)
        for key, vals in aliases.items():
            if key in res:
                continue
            if any(_norm_alias(v) in n for v in vals):
                res[key] = f
    return res


def _parallax_from_height(img):
    if img is None:
        return None
    h = img.astype(np.float32)
    if h.ndim == 3:
        h = cv2.cvtColor(h[:, :, :3], cv2.COLOR_BGR2GRAY)
    h = cv2.normalize(h, None, 0, 255, cv2.NORM_MINMAX)
    h = 127 + (h - h.mean()) * 0.4
    return np.clip(h, 60, 200).astype(np.uint8)


def _build_rmaos_from_channels(roughness, metalness, ao, specular):
    if roughness is None:
        return None
    h, w = roughness.shape[:2]

    def _resize(m):
        return cv2.resize(m, (w, h), cv2.INTER_LINEAR) if m is not None and m.shape[:2] != (h, w) else m

    metalness = _resize(metalness)
    ao        = _resize(ao)
    specular  = _resize(specular)

    g = metalness if metalness is not None else np.zeros((h, w), np.uint8)
    b = ao        if ao is not None        else np.full((h, w), 255, np.uint8)
    a = specular  if specular is not None  else (255 - roughness)

    out = np.zeros((h, w, 4), np.uint8)
    out[..., 0] = b         # AO → blue
    out[..., 1] = g         # metallic → green
    out[..., 2] = roughness # roughness → red
    out[..., 3] = a         # specular → alpha
    return out


def run_build_pbr(src_dir: str, out_dir: str, flip_green: bool,
                  aliases_override: dict = None,
                  log: Callable = None,
                  progress: Callable = None,
                  cancelled: Callable = None):
    """
    Build PBR DDS textures from loose PBR maps.
    aliases_override: dict merging with default aliases (optional)
    log(msg, color): callback for output
    progress(done, total): callback
    cancelled(): returns True if user cancelled
    """
    def _log(msg, c=None): log and log(msg, c)
    def _prog(d, t): progress and progress(d, t)
    def _is_cancelled(): return cancelled and cancelled()

    src, out = Path(src_dir), Path(out_dir)
    aliases = copy.deepcopy(_BUILD_PBR_ALIASES_DEFAULT)
    if aliases_override:
        deep_update(aliases, aliases_override)

    tmp = out / '_tmp'
    tmp.mkdir(parents=True, exist_ok=True)

    files = [f for f in src.rglob('*')
             if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS]
    if not files:
        _log(f'No texture files found in: {src}', 'warn')
        return 0

    grouped = _group_by_base(files)
    total = len(grouped)
    _log(f'Found {len(files)} file(s) in {total} texture set(s).\n')
    _prog(0, total)

    count = 0
    for done, (name, mats) in enumerate(grouped.items(), 1):
        if _is_cancelled():
            _log('Cancelled.', 'warn')
            break

        m = _match_aliases(mats, aliases)
        albedo = m.get('albedo')
        normal = m.get('normal')
        if not albedo or not normal:
            _log(f'skip {name} – missing albedo or normal', 'warn')
            _prog(done, total)
            continue

        _log(f'Processing: {name}')

        # Albedo → BC7 sRGB
        a_png = image_to_png(albedo, tmp)
        if a_png:
            png_to_dds(a_png, out / f'{name}.dds', 'BC7_UNORM', srgb=True)

        # Normal → BC7 linear (optional Y-flip)
        n_png = image_to_png(normal, tmp)
        if n_png:
            n = cv2.imread(str(n_png), cv2.IMREAD_UNCHANGED)
            if n is not None and flip_green and n.ndim == 3 and n.shape[2] >= 2:
                n[:, :, 1] = 255 - n[:, :, 1]
                n_out = tmp / f'{name}_n_flipped.png'
                write_png(n_out, n)
                n_png = n_out
            png_to_dds(n_png, out / f'{name}_n.dds', 'BC7_UNORM', srgb=False)

        # Height → BC4 linear
        height_src = m.get('height')
        if height_src:
            h_png = image_to_png(height_src, tmp)
            if h_png:
                h = cv2.imread(str(h_png), cv2.IMREAD_UNCHANGED)
                h = _parallax_from_height(h)
                if h is not None:
                    hp = tmp / f'{name}_p.png'
                    write_png(hp, h)
                    png_to_dds(hp, out / f'{name}_p.dds', 'BC4_UNORM', srgb=False)

        # RMAOS → BC7 linear
        rough_src = m.get('roughness')
        if rough_src:
            r_img, _ = read_image(rough_src,   tmp, cv2.IMREAD_GRAYSCALE)
            me_img,_ = read_image(m['metalness'],tmp,cv2.IMREAD_GRAYSCALE) if m.get('metalness') else (None,None)
            ao_img,_ = read_image(m['ao'],      tmp, cv2.IMREAD_GRAYSCALE) if m.get('ao')        else (None,None)
            sp_img,_ = read_image(m['specular'],tmp,cv2.IMREAD_GRAYSCALE)  if m.get('specular')  else (None,None)
            rma = _build_rmaos_from_channels(r_img, me_img, ao_img, sp_img)
            if rma is not None:
                rp = tmp / f'{name}_rmaos.png'
                write_png(rp, rma)
                png_to_dds(rp, out / f'{name}_rmaos.dds', 'BC7_UNORM', srgb=False)

        _log(f'  ✓ {name}', 'success')
        count += 1
        _prog(done, total)

    shutil.rmtree(tmp, ignore_errors=True)
    return count


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 2 – PARALLAX GENERATOR  (generate_parallax.py)
# ═════════════════════════════════════════════════════════════════════════════

_PARALLAX_CONFIG_DEFAULT = {
    'default': {
        'contrast_factor': 0.4,
        'clamp_low':       60,
        'clamp_high':      200,
        'blur_radius':     4,
        'red_value':       75,
        'green_value':     65,
        'blue_value':      75,
        'red_brightness':  1.0,
        'green_brightness':1.0,
        'blue_brightness': 1.0,
    },
    'categories': {'wood': {'contrast_factor': 0.3}, 'path': {'contrast_factor': 0.5}},
    'overrides':  {},
    'exclude':    [],
}


def _luminance_bgr(img):
    if img.ndim == 2:
        return img.astype(np.float32)
    rgb = img[:, :, :3].astype(np.float32)
    return 0.0722 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.2126 * rgb[..., 2]


def _raw_normal_to_height(normal):
    """FFT-based normal map → height map."""
    if normal is None:
        return None
    if normal.ndim == 2:
        normal = cv2.cvtColor(normal, cv2.COLOR_GRAY2BGR)
    img = normal[:, :, :3].astype(np.float32) / 255.0
    nx = img[:, :, 2] * 2.0 - 1.0   # R channel
    ny = img[:, :, 1] * 2.0 - 1.0   # G channel
    gx, gy = -nx, -ny
    h, w = gx.shape
    fx, fy = np.fft.fft2(gx), np.fft.fft2(gy)
    wx = 2.0 * np.pi * np.fft.fftfreq(w).reshape(1, w)
    wy = 2.0 * np.pi * np.fft.fftfreq(h).reshape(h, 1)
    denom = wx ** 2 + wy ** 2
    denom[0, 0] = 1.0
    fz = (-1j * wx * fx - 1j * wy * fy) / denom
    fz[0, 0] = 0.0
    hmap = np.real(np.fft.ifft2(fz))
    hmap -= hmap.min()
    if hmap.max() > 0:
        hmap /= hmap.max()
    return hmap


def _apply_height_settings(height, settings):
    if height is None:
        return None
    h = height.astype(np.float32)
    if h.max() <= 1.0:
        h *= 255.0
    mean = h.mean()
    contrast = float(settings.get('contrast_factor', 0.5))
    h = 127.0 + (h - mean) * contrast
    lo, hi = int(settings.get('clamp_low', 60)), int(settings.get('clamp_high', 200))
    if hi <= lo:
        lo, hi = 0, 255
    h = np.clip(h, lo, hi)
    blur = float(settings.get('blur_radius', 4))
    if blur > 0:
        sigma = (blur ** 1.6) * 0.9
        h = cv2.GaussianBlur(h, (0, 0), sigmaX=sigma, sigmaY=sigma,
                             borderType=cv2.BORDER_REFLECT_101)
    return np.ascontiguousarray(np.clip(h, 0, 255).astype(np.uint8))


def _normal_to_height(normal, settings):
    return _apply_height_settings(_raw_normal_to_height(normal), settings)


def _build_complex_m_parallax(diffuse, height, settings):
    h, w = diffuse.shape[:2]
    if height.shape[:2] != (h, w):
        height = cv2.resize(height, (w, h), cv2.INTER_LINEAR)

    def ch(val_key, br_key):
        return int(np.clip(float(settings.get(val_key, 0)) *
                           float(settings.get(br_key, 1.0)), 0, 255))

    out = np.zeros((h, w, 4), np.uint8)
    out[:, :, 0] = ch('blue_value',  'blue_brightness')
    out[:, :, 1] = ch('green_value', 'green_brightness')
    out[:, :, 2] = ch('red_value',   'red_brightness')
    out[:, :, 3] = height
    return out


def _build_rmaos_parallax(diffuse, normal):
    h, w = diffuse.shape[:2]
    if normal.shape[:2] != (h, w):
        normal = cv2.resize(normal, (w, h), cv2.INTER_LINEAR)
    diff_luma = _luminance_bgr(diffuse) / 255.0
    rough = np.clip(1.0 - diff_luma, 0.05, 0.95)
    metal = np.zeros((h, w), np.float32)
    n_rgb = normal[:, :, :3] if normal.ndim == 3 else cv2.cvtColor(normal, cv2.COLOR_GRAY2BGR)
    n = n_rgb.astype(np.float32) / 255.0 * 2.0 - 1.0
    dx, dy = np.gradient(n, axis=1), np.gradient(n, axis=0)
    curv = np.sqrt(np.sum(dx * dx + dy * dy, axis=2))
    curv /= (np.max(curv) + 1e-5)
    ao = np.clip(1.0 - cv2.GaussianBlur(curv, (3, 3), 0), 0.45, 1.0)
    spec = 1.0 - rough
    out = np.zeros((h, w, 4), np.uint8)
    out[:, :, 0] = (ao * 255).astype(np.uint8)
    out[:, :, 1] = (metal * 255).astype(np.uint8)
    out[:, :, 2] = (rough * 255).astype(np.uint8)
    out[:, :, 3] = (spec * 255).astype(np.uint8)
    return out


def _pbr_rel_dir(rel_dir: Path) -> Path:
    parts = rel_dir.parts
    if parts and parts[0].lower() == 'textures':
        return Path('textures') / 'PBR' / Path(*parts[1:]) if len(parts) > 1 else Path('textures/PBR')
    return Path('textures') / 'PBR' / rel_dir


def _should_exclude(name, rel_key, config):
    for item in config.get('exclude', []):
        needle = str(item).lower()
        if needle and (needle in name or needle in rel_key):
            return True
    return False


def _settings_for_texture(name, rel_key, config):
    settings = copy.deepcopy(config.get('default', {}))
    for cat, vals in config.get('categories', {}).items():
        if str(cat).lower() in rel_key or str(cat).lower() in name:
            if isinstance(vals, dict):
                deep_update(settings, vals)
    for ov_name, vals in config.get('overrides', {}).items():
        if str(ov_name).lower() in (name, rel_key):
            if isinstance(vals, dict):
                deep_update(settings, vals)
    return settings


def _texture_identity(parts: dict, src: Path):
    dp = parts.get('albedo')
    if not dp:
        return '', ''
    stem = dp.stem
    if stem.lower().endswith('_d'):
        stem = stem[:-2]
    try:
        rel_dir = dp.parent.relative_to(src)
        rel_key = str(rel_dir / stem).replace('/', '\\').lower()
    except ValueError:
        rel_key = stem.lower()
    return stem.lower(), rel_key


def run_generate_parallax(src_dir: str, out_dir: str, mode: str,
                          config_override: dict = None,
                          log: Callable = None,
                          progress: Callable = None,
                          cancelled: Callable = None):
    """
    mode: 'complex' | 'pbr' | 'both'
    config_override: merged with default parallax config
    """
    def _log(msg, c=None): log and log(msg, c)
    def _prog(d, t): progress and progress(d, t)
    def _is_cancelled(): return cancelled and cancelled()

    config = copy.deepcopy(_PARALLAX_CONFIG_DEFAULT)
    if config_override:
        deep_update(config, config_override)

    src, out = Path(src_dir), Path(out_dir)
    tmp = out / '_tmp'
    tmp.mkdir(parents=True, exist_ok=True)

    raw_sets = scan_texture_sets(src)
    sets = {}
    for key, parts in raw_sets.items():
        name_key, rel_key = _texture_identity(parts, src)
        if _should_exclude(name_key, rel_key, config):
            _log(f'Excluded: {rel_key}', 'warn')
            continue
        sets[key] = parts

    total = len(sets)
    _log(f'Found {total} texture set(s) (mode: {mode}).\n')
    _prog(0, total)
    count = 0

    for done, (_, parts) in enumerate(sets.items(), 1):
        if _is_cancelled():
            _log('Cancelled.', 'warn')
            break

        diffuse_path = parts.get('albedo')
        normal_path  = parts.get('normal')
        if not diffuse_path or not normal_path:
            _prog(done, total)
            continue

        rel_dir = diffuse_path.parent.relative_to(src)
        stem = diffuse_path.stem
        if stem.lower().endswith('_d'):
            stem = stem[:-2]
        name_key, rel_key = _texture_identity(parts, src)
        settings = _settings_for_texture(name_key, rel_key, config)

        _log(f'Processing: {rel_dir / stem}')

        complex_out = (out / rel_dir)
        complex_out.mkdir(parents=True, exist_ok=True)
        pbr_out = (out / _pbr_rel_dir(rel_dir))
        pbr_out.mkdir(parents=True, exist_ok=True)

        diffuse, _ = read_image(diffuse_path, tmp, cv2.IMREAD_UNCHANGED)
        normal,  _ = read_image(normal_path,  tmp, cv2.IMREAD_UNCHANGED)
        if diffuse is None or normal is None:
            _log('  skipped – could not read diffuse or normal', 'warn')
            _prog(done, total)
            continue

        if mode in ('complex', 'both'):
            copy_or_convert_to_dds(diffuse_path, complex_out / f'{stem}.dds',   'BC7_UNORM', True,  tmp)
            copy_or_convert_to_dds(normal_path,  complex_out / f'{stem}_n.dds', 'BC7_UNORM', False, tmp)
        if mode in ('pbr', 'both'):
            copy_or_convert_to_dds(diffuse_path, pbr_out / f'{stem}.dds',   'BC7_UNORM', True,  tmp)
            copy_or_convert_to_dds(normal_path,  pbr_out / f'{stem}_n.dds', 'BC7_UNORM', False, tmp)

        # Height
        height_path = parts.get('height')
        if height_path:
            h_raw, _ = read_image(height_path, tmp, cv2.IMREAD_GRAYSCALE)
            height = _apply_height_settings(h_raw, settings)
        else:
            height = _normal_to_height(normal, settings)

        if height is None:
            _log('  skipped – could not generate height', 'warn')
            _prog(done, total)
            continue

        p_png = tmp / f'{stem}_p.png'
        write_png(p_png, height)

        if mode in ('complex', 'both'):
            png_to_dds(p_png, complex_out / f'{stem}_p.dds', 'BC4_UNORM', srgb=False)
        if mode in ('pbr', 'both'):
            png_to_dds(p_png, pbr_out / f'{stem}_p.dds', 'BC4_UNORM', srgb=False)

        # Complex _m
        if mode in ('complex', 'both'):
            existing_m = parts.get('complex_m')
            if existing_m:
                copy_or_convert_to_dds(existing_m, complex_out / f'{stem}_m.dds', 'BC7_UNORM', False, tmp)
            else:
                m_map = _build_complex_m_parallax(diffuse, height, settings)
                m_png = tmp / f'{stem}_m.png'
                write_png(m_png, m_map)
                png_to_dds(m_png, complex_out / f'{stem}_m.dds', 'BC7_UNORM', srgb=False)

        # RMAOS
        if mode in ('pbr', 'both'):
            existing_rmaos = parts.get('rmaos')
            if existing_rmaos:
                copy_or_convert_to_dds(existing_rmaos, pbr_out / f'{stem}_rmaos.dds', 'BC7_UNORM', False, tmp)
            else:
                rmaos = _build_rmaos_parallax(diffuse, normal)
                r_png = tmp / f'{stem}_rmaos.png'
                write_png(r_png, rmaos)
                png_to_dds(r_png, pbr_out / f'{stem}_rmaos.dds', 'BC7_UNORM', srgb=False)

        _log(f'  ✓ {stem}', 'success')
        count += 1
        _prog(done, total)

    shutil.rmtree(tmp, ignore_errors=True)
    return count


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 3 – PBR JSON GENERATOR  (generate.py)
# ═════════════════════════════════════════════════════════════════════════════

_GENERATE_CONFIG_DEFAULT = {
    'defaults': {
        'emissive':         False,
        'parallax':         True,
        'subsurface':       False,
        'smooth_angle':     75,
        'specular_level':   0.02,
        'subsurface_color': [1, 1, 1],
        'roughness_scale':  1,
        'subsurface_opacity': 1,
        'displacement_scale': 0.4,
        'glint': {
            'screen_space_scale':     0,
            'log_microfacet_density': 0,
            'microfacet_roughness':   0,
            'density_randomization':  0,
        },
    },
    'keywords':       {},
    'file_overrides': {},
}

_GENERATE_REQUIRED = {'diffuse', 'normal', 'height', 'rmaos'}

_GENERATE_SUFFIX_MAP = [
    ('_rmaos', 'rmaos'),
    ('_n',     'normal'),
    ('_p',     'height'),
    ('_d',     'diffuse'),
]


def _gen_strip_suffix(stem: str):
    lower = stem.lower()
    for suffix, channel in _GENERATE_SUFFIX_MAP:
        if lower.endswith(suffix):
            return stem[:-len(suffix)], channel
    return stem, 'diffuse'


def _gen_texture_key(path: Path, mod_root: Path):
    rel = path.relative_to(mod_root)
    parts = list(rel.parts)
    if parts and parts[0].lower() == 'textures':
        parts = parts[1:]
    if parts and parts[0].lower() == 'pbr':
        parts = parts[1:]
    stem, channel = _gen_strip_suffix(Path(parts[-1]).stem)
    parts[-1] = stem
    key = str(Path(*parts)).replace('/', '\\')
    return key, channel


def _gen_scan_sets(mod_root: Path):
    sets: Dict[str, dict] = {}
    for path in mod_root.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTS:
            continue
        key, channel = _gen_texture_key(path, mod_root)
        sets.setdefault(key, {})[channel] = path
    complete, skipped = {}, {}
    for key, channels in sets.items():
        missing = _GENERATE_REQUIRED - set(channels.keys())
        if missing:
            skipped[key] = sorted(missing)
        else:
            complete[key] = channels
    return complete, skipped


def _gen_setting_from_value(value):
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return {'displacement_scale': value}


def _gen_settings_for(texture_key: str, config: dict):
    settings = copy.deepcopy(config.get('defaults', {}))
    lower = texture_key.lower()
    file_name = lower.split('\\')[-1]
    for kw, val in config.get('keywords', {}).items():
        if str(kw).lower() in lower:
            deep_update(settings, _gen_setting_from_value(val))
    for ov_key, val in config.get('file_overrides', {}).items():
        norm = str(ov_key).replace('/', '\\').lower()
        if norm in (lower, file_name):
            deep_update(settings, _gen_setting_from_value(val))
    return settings


def run_generate_json(mod_dir: str, json_name: str,
                      config_override: dict = None,
                      log: Callable = None,
                      progress: Callable = None,
                      cancelled: Callable = None):
    """
    Scan mod_dir for complete PBR texture sets (diffuse+normal+height+rmaos)
    and write a PBRNIFPatcher JSON to mod_dir/PBRNIFPatcher/<json_name>.json
    """
    def _log(msg, c=None): log and log(msg, c)
    def _prog(d, t): progress and progress(d, t)
    def _is_cancelled(): return cancelled and cancelled()

    config = copy.deepcopy(_GENERATE_CONFIG_DEFAULT)
    if config_override:
        deep_update(config, config_override)

    mod_root = Path(mod_dir)
    if not json_name.lower().endswith('.json'):
        json_name += '.json'

    out_folder = mod_root / 'PBRNIFPatcher'
    out_folder.mkdir(parents=True, exist_ok=True)
    out_path = out_folder / json_name

    texture_sets, skipped = _gen_scan_sets(mod_root)
    total = len(texture_sets)
    _log(f'Found {total} complete texture set(s).')
    if skipped:
        _log(f'Skipping {len(skipped)} incomplete set(s).')
    _prog(0, total)

    entries = []
    for done, tex_key in enumerate(sorted(texture_sets.keys()), 1):
        if _is_cancelled():
            _log('Cancelled.', 'warn')
            break
        _log(f'Adding: {tex_key}')
        entry = {'texture': tex_key}
        deep_update(entry, _gen_settings_for(tex_key, config))
        entries.append(entry)
        _prog(done, total)

    with out_path.open('w', encoding='utf-8') as f:
        json.dump(entries, f, indent=4)
        f.write('\n')

    if skipped:
        _log(f'\nSkipped sets (missing channels):', 'warn')
        for key, missing in sorted(skipped.items()):
            _log(f'  {key} – missing: {", ".join(missing)}', 'warn')

    return len(entries), str(out_path)


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 3b – PBR JSON GENERATOR, Step1.ps1 + Step2.ps1 port
#
# This is a *separate* generator from run_generate_json() above, faithfully
# ported from the original two-stage PowerShell pipeline rather than reusing
# generate.py's logic. Key differences from run_generate_json():
#   - Writes ONE JSON FILE PER TEXTURE (mirroring the source folder layout
#     under PBRNifPatcher/), not one combined JSON for the whole mod.
#   - Only requires a diffuse image to exist (Step1's rule) -- normal/height/
#     rmaos are all optional and only affect which flags get set.
#   - Reproduces the scripts' "_d" convention: a diffuse file ending in "_d"
#     (e.g. "wood_d.dds") is looked up using the base name with "_d" stripped
#     (to find "wood_g.dds" etc.), but the JSON's "texture" field keeps the
#     "_d" and a "rename" field is added pointing at the stripped name --
#     then the diffuse file itself is renamed on disk to drop "_d", exactly
#     as Step2.ps1 does at the end of its run.
# ═════════════════════════════════════════════════════════════════════════════

_PS1_EXCLUDE_SUFFIXES = ('_n', '_g', '_s', '_cnr', '_f', '_p', '_rmaos')


def run_generate_json_ps1_style(mod_dir: str,
                                 textures_subdir: str = 'Textures/PBR',
                                 output_subdir: str = 'PBRNifPatcher',
                                 log: Callable = None,
                                 progress: Callable = None,
                                 cancelled: Callable = None):
    """
    Python port of Step1.ps1 + Step2.ps1: scan <mod_dir>/<textures_subdir> for
    diffuse textures, and write one JSON per texture under
    <mod_dir>/<output_subdir>/, mirroring the source folder structure.
    """
    def _log(msg, c=None): log and log(msg, c)
    def _prog(d, t): progress and progress(d, t)
    def _is_cancelled(): return cancelled and cancelled()

    mod_root = Path(mod_dir)
    root_folder = mod_root / textures_subdir.replace('/', os.sep)
    out_folder = mod_root / output_subdir

    if not root_folder.is_dir():
        _log(f'Folder not found: {root_folder}', 'error')
        return 0, str(out_folder)

    all_files = [p for p in root_folder.rglob('*')
                 if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]
    diffuse_files = [p for p in all_files
                      if not p.stem.lower().endswith(_PS1_EXCLUDE_SUFFIXES)]

    total = len(diffuse_files)
    _log(f'Found {total} diffuse texture(s) under {root_folder.name}.')
    _prog(0, total)

    written = 0
    renamed = 0

    for i, dpath in enumerate(sorted(diffuse_files), 1):
        if _is_cancelled():
            _log('Cancelled.', 'warn')
            break

        stem = dpath.stem
        folder = dpath.parent
        rel_dir = folder.relative_to(root_folder)

        is_d = stem.lower().endswith('_d')
        lookup_base = stem[:-2] if is_d else stem

        def _sibling_exists(suffix):
            return (folder / f'{lookup_base}{suffix}{dpath.suffix}').is_file()

        has_glow       = _sibling_exists('_g')
        has_height     = _sibling_exists('_p')
        has_subsurface = _sibling_exists('_s')
        has_coat       = _sibling_exists('_cnr')
        has_fuzz       = _sibling_exists('_f')

        def _relkey(name):
            return str(rel_dir / name).replace('\\', '/').lstrip('./').replace('/', '\\') \
                if str(rel_dir) != '.' else name

        entry = {'texture': _relkey(stem)}
        if is_d:
            entry['rename'] = _relkey(lookup_base)

        entry.update({
            'emissive': has_glow,
            'parallax': has_height,
            'subsurface_foliage': False,
            'subsurface': bool(has_subsurface and not has_coat),
            'specular_level': 0.04,
            'subsurface_color': [1, 1, 1],
            'roughness_scale': 1,
            'subsurface_opacity': 1,
            # Step2.ps1 literally hardcodes this to boolean `false` rather than
            # a numeric angle -- almost certainly a bug in the original script,
            # but reproduced as-is here for a faithful port. Flag for review.
            'smooth_angle': False,
            'displacement_scale': 2 if has_coat else 1,
        })
        if has_coat:
            entry.update({
                'multilayer': True,
                'coat_diffuse': True,
                'coat_normal': True,
                'coat_parallax': True,
                'coat_strength': 1.0,
                'coat_roughness': 1.0,
                'coat_specular_level': 0.018,
            })
        if has_fuzz:
            entry['fuzz'] = {'texture': True}

        out_dir = out_folder / rel_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f'{stem}.json'
        with out_path.open('w', encoding='utf-8') as f:
            json.dump([entry], f, indent=4)
            f.write('\n')
        written += 1
        _log(f'Wrote {out_path.relative_to(mod_root)}')

        if is_d:
            new_path = dpath.with_name(f'{lookup_base}{dpath.suffix}')
            if not new_path.exists():
                try:
                    dpath.rename(new_path)
                    renamed += 1
                    _log(f'  renamed {dpath.name} -> {new_path.name}')
                except OSError as e:
                    _log(f'  rename failed for {dpath.name}: {e}', 'error')

        _prog(i, total)

    _log(f'\nDone -- wrote {written} JSON file(s), renamed {renamed} diffuse file(s).')
    return written, str(out_folder)


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 4 – COMPLEX PARALLAX → PBR  (convert.py)
# ═════════════════════════════════════════════════════════════════════════════

def _conv_luminance_bgr(img):
    rgb = img[:, :, :3].astype(np.float32)
    return 0.0722 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.2126 * rgb[..., 2]


def _conv_blur_3x3(img):
    kernel = np.array([[1,2,1],[2,4,2],[1,2,1]], np.float32) / 16.0
    padded = np.pad(img, ((1,1),(1,1)), mode='edge')
    out = np.zeros_like(img)
    for y in range(3):
        for x in range(3):
            out += kernel[y,x] * padded[y:y+img.shape[0], x:x+img.shape[1]]
    return out


def _conv_build_rmaos(diffuse, normal, complex_m):
    h, w = diffuse.shape[:2]
    for src in (normal, complex_m):
        if src is not None and src.shape[:2] != (h, w):
            src = cv2.resize(src, (w, h), cv2.INTER_LINEAR)

    diff_rgb = diffuse[:, :, :3] if diffuse.ndim == 3 else cv2.cvtColor(diffuse, cv2.COLOR_GRAY2BGR)
    rough = np.clip(1.0 - (_conv_luminance_bgr(diff_rgb) / 255.0), 0.05, 0.95)

    if complex_m is not None and complex_m.ndim == 3 and complex_m.shape[2] >= 3:
        metal = complex_m[..., 0].astype(np.float32) / 255.0
    else:
        metal = np.zeros((h, w), np.float32)

    norm_rgb = normal[:, :, :3] if normal is not None and normal.ndim == 3 \
               else cv2.cvtColor(normal, cv2.COLOR_GRAY2BGR) if normal is not None \
               else np.full((h, w, 3), 127, np.uint8)
    n = norm_rgb.astype(np.float32) / 255.0 * 2.0 - 1.0
    dx, dy = np.gradient(n, axis=1), np.gradient(n, axis=0)
    curvature = np.sqrt(np.sum(dx*dx + dy*dy, axis=2))
    curvature /= (np.max(curvature) + 1e-5)
    ao = np.clip(1.0 - _conv_blur_3x3(curvature), 0.45, 1.0)
    spec = 1.0 - rough

    out = np.zeros((h, w, 4), np.uint8)
    out[:, :, 0] = (ao * 255).astype(np.uint8)
    out[:, :, 1] = (metal * 255).astype(np.uint8)
    out[:, :, 2] = (rough * 255).astype(np.uint8)
    out[:, :, 3] = (spec * 255).astype(np.uint8)
    return out


def _conv_extract_height_from_m(complex_m):
    if complex_m is None:
        return None
    if complex_m.ndim == 3 and complex_m.shape[2] >= 4:
        return complex_m[:, :, 3].astype(np.uint8)
    if complex_m.ndim == 2:
        return complex_m.astype(np.uint8)
    return cv2.cvtColor(complex_m[:, :, :3], cv2.COLOR_BGR2GRAY)


def run_convert_to_pbr(src_dir: str, out_dir: str,
                       log: Callable = None,
                       progress: Callable = None,
                       cancelled: Callable = None):
    """Convert Complex Parallax texture sets → Community Shaders PBR."""
    def _log(msg, c=None): log and log(msg, c)
    def _prog(d, t): progress and progress(d, t)
    def _is_cancelled(): return cancelled and cancelled()

    src, out = Path(src_dir), Path(out_dir)
    tmp = out / '_tmp'
    tmp.mkdir(parents=True, exist_ok=True)

    sets = scan_texture_sets(src)
    # Need albedo + normal + complex_m
    valid = {k: v for k, v in sets.items()
             if v.get('albedo') and v.get('normal') and v.get('complex_m')}
    total = len(valid)
    _log(f'Found {total} complete Complex Parallax set(s).\n')
    _prog(0, total)
    count = 0

    for done, (_, parts) in enumerate(valid.items(), 1):
        if _is_cancelled():
            _log('Cancelled.', 'warn')
            break

        diffuse_path = parts['albedo']
        normal_path  = parts['normal']
        m_path       = parts['complex_m']

        rel_dir = diffuse_path.parent.relative_to(src)
        pbr_rel = _pbr_rel_dir(rel_dir)
        out_d = out / pbr_rel
        out_d.mkdir(parents=True, exist_ok=True)

        stem = diffuse_path.stem
        if stem.lower().endswith('_d'):
            stem = stem[:-2]

        _log(f'Processing: {rel_dir / stem}')

        copy_or_convert_to_dds(diffuse_path, out_d / f'{stem}.dds',   'BC7_UNORM', True,  tmp)
        copy_or_convert_to_dds(normal_path,  out_d / f'{stem}_n.dds', 'BC7_UNORM', False, tmp)

        diffuse,   _ = read_image(diffuse_path, tmp, cv2.IMREAD_UNCHANGED)
        normal,    _ = read_image(normal_path,  tmp, cv2.IMREAD_UNCHANGED)
        complex_m, _ = read_image(m_path,       tmp, cv2.IMREAD_UNCHANGED)

        if diffuse is None or normal is None or complex_m is None:
            _log('  skipped – could not read one or more textures', 'warn')
            _prog(done, total)
            continue

        height = _conv_extract_height_from_m(complex_m)
        p_png = tmp / f'{stem}_p.png'
        write_png(p_png, height)
        png_to_dds(p_png, out_d / f'{stem}_p.dds', 'BC4_UNORM', srgb=False)

        rmaos = _conv_build_rmaos(diffuse, normal, complex_m)
        r_png = tmp / f'{stem}_rmaos.png'
        write_png(r_png, rmaos)
        png_to_dds(r_png, out_d / f'{stem}_rmaos.dds', 'BC7_UNORM', srgb=False)

        _log(f'  ✓ {stem}', 'success')
        count += 1
        _prog(done, total)

    shutil.rmtree(tmp, ignore_errors=True)
    return count


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 5 – PBR → COMPLEX PARALLAX  (convert_pbr_to_complex.py)
# ═════════════════════════════════════════════════════════════════════════════

def _build_complex_m_from_pbr(diffuse, height, rmaos=None):
    h, w = diffuse.shape[:2]
    if height.shape[:2] != (h, w):
        height = cv2.resize(height, (w, h), cv2.INTER_LINEAR)

    diff_rgb = diffuse[:, :, :3] if diffuse.ndim == 3 else cv2.cvtColor(diffuse, cv2.COLOR_GRAY2BGR)
    green = diff_rgb[:, :, 1].astype(np.float32)
    green = np.clip(green + (65 - green.mean()), 0, 255).astype(np.uint8)

    red  = np.zeros((h, w), np.uint8)
    blue = np.zeros((h, w), np.uint8)

    if rmaos is not None and rmaos.ndim == 3 and rmaos.shape[2] >= 2:
        if rmaos.shape[:2] != (h, w):
            rmaos = cv2.resize(rmaos, (w, h), cv2.INTER_LINEAR)
        blue = rmaos[:, :, 1]
        red  = rmaos[:, :, 1]

    out = np.zeros((h, w, 4), np.uint8)
    out[:, :, 0] = blue
    out[:, :, 1] = green
    out[:, :, 2] = red
    out[:, :, 3] = height
    return out


def run_convert_to_complex(src_dir: str, out_dir: str,
                           log: Callable = None,
                           progress: Callable = None,
                           cancelled: Callable = None):
    """Convert PBR texture sets → Complex Parallax _m format."""
    def _log(msg, c=None): log and log(msg, c)
    def _prog(d, t): progress and progress(d, t)
    def _is_cancelled(): return cancelled and cancelled()

    src, out = Path(src_dir), Path(out_dir)
    tmp = out / '_tmp'
    tmp.mkdir(parents=True, exist_ok=True)

    sets = scan_texture_sets(src)
    valid = {k: v for k, v in sets.items()
             if v.get('albedo') and v.get('normal') and v.get('height')}
    total = len(valid)
    _log(f'Found {total} PBR set(s) with height maps.\n')
    _prog(0, total)
    count = 0

    for done, (_, parts) in enumerate(valid.items(), 1):
        if _is_cancelled():
            _log('Cancelled.', 'warn')
            break

        diffuse_path = parts['albedo']
        normal_path  = parts['normal']
        height_path  = parts['height']
        rmaos_path   = parts.get('rmaos')

        rel_dir = diffuse_path.parent.relative_to(src)
        out_d = out / rel_dir
        out_d.mkdir(parents=True, exist_ok=True)

        stem = diffuse_path.stem
        if stem.lower().endswith('_d'):
            stem = stem[:-2]

        _log(f'Processing: {rel_dir / stem}')

        copy_or_convert_to_dds(diffuse_path, out_d / f'{stem}.dds',   'BC7_UNORM', True,  tmp)
        copy_or_convert_to_dds(normal_path,  out_d / f'{stem}_n.dds', 'BC7_UNORM', False, tmp)
        copy_or_convert_to_dds(height_path,  out_d / f'{stem}_p.dds', 'BC4_UNORM', False, tmp)

        diffuse,_ = read_image(diffuse_path, tmp, cv2.IMREAD_UNCHANGED)
        height, _ = read_image(height_path,  tmp, cv2.IMREAD_GRAYSCALE)
        rmaos,  _ = read_image(rmaos_path,   tmp, cv2.IMREAD_UNCHANGED) if rmaos_path else (None, None)

        if diffuse is None or height is None:
            _log('  skipped – could not read diffuse or height', 'warn')
            _prog(done, total)
            continue

        m_map = _build_complex_m_from_pbr(diffuse, height, rmaos)
        m_png = tmp / f'{stem}_m.png'
        write_png(m_png, m_map)
        png_to_dds(m_png, out_d / f'{stem}_m.dds', 'BC7_UNORM', srgb=False)

        _log(f'  ✓ {stem}', 'success')
        count += 1
        _prog(done, total)

    shutil.rmtree(tmp, ignore_errors=True)
    return count
