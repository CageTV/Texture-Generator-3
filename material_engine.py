"""
Material Engine
Generate a full PBR map set from a single diffuse/albedo texture.
Algorithms inspired by Materialize (BoundingBoxSoftware), reimplemented in Python/NumPy/OpenCV.

Maps generated:
  Height      – luminance-based displacement
  Normal      – gradient/Sobel surface normals from height
  AO          – ambient occlusion from height curvature
  Roughness   – inverse-luminance + saturation weighting
  Metalness   – bright-value + low-saturation detector
  Edge        – Canny edge / mask map
  Emissive    – bright-area threshold glow map
"""

import numpy as np
import cv2

try:
    import ai_depth_engine as _ai
    _AI_OK = _ai.is_ai_available()
except Exception as _ai_e:
    _AI_OK = False
    _ai = None
    print(f"[Material Engine] AI depth disabled: {_ai_e}")
from PIL import Image
from pathlib import Path
from typing import Callable, Optional, Dict


# ── Image helpers ─────────────────────────────────────────────────────────────

def _load(path) -> Optional[np.ndarray]:
    """Load image as BGR uint8 numpy array. Falls back to PIL for exotic formats."""
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        try:
            pil = Image.open(str(path)).convert('RGBA')
            img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGBA2BGRA)
        except Exception:
            return None
    # Ensure at least 3 channels
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


def _gray(img: np.ndarray) -> np.ndarray:
    """Return float32 grayscale [0–255]."""
    if img.ndim == 2:
        return img.astype(np.float32)
    return cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32)


def _blur(arr: np.ndarray, radius: float) -> np.ndarray:
    if radius <= 0:
        return arr
    return cv2.GaussianBlur(arr.astype(np.float32), (0, 0),
                            sigmaX=radius, sigmaY=radius,
                            borderType=cv2.BORDER_REFLECT_101)


def _save(img: np.ndarray, path: Path):
    """Save numpy array via PIL (handles both grayscale and colour)."""
    if img.ndim == 2:
        Image.fromarray(img).save(str(path))
    elif img.shape[2] == 3:
        Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).save(str(path))
    else:
        Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)).save(str(path))


# ═════════════════════════════════════════════════════════════════════════════
# MAP GENERATORS
# ═════════════════════════════════════════════════════════════════════════════

def adjust_diffuse(img: np.ndarray,
                    brightness: float = 0.0,
                    contrast: float = 1.0,
                    saturation: float = 1.0) -> np.ndarray:
    """
    Pre-processing pass applied to the diffuse/albedo source before any other
    map is derived from it (Materialize applies the same idea: brightness/
    contrast/saturation on the working copy feed into every downstream map).
    brightness  – additive offset, -1..+1
    contrast    – multiplier around the 0.5 midpoint
    saturation  – HSV saturation multiplier (1.0 = unchanged, 0 = grayscale)
    """
    out = img.astype(np.float32) / 255.0
    rgb = out[:, :, :3]

    if abs(saturation - 1.0) > 1e-6:
        hsv = cv2.cvtColor(rgb, cv2.COLOR_BGR2HSV)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * saturation, 0, 1)
        rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    rgb = np.clip((rgb - 0.5) * contrast + 0.5 + brightness, 0, 1)
    out[:, :, :3] = rgb
    return np.clip(out * 255, 0, 255).astype(np.uint8)


def height_from_diffuse(img: np.ndarray,
                        blur_radius: float = 2.0,
                        contrast: float = 1.0,
                        brightness: float = 0.0,
                        invert: bool = False,
                        use_ai: bool = False,
                        ai_model: str = "depth-anything-small",
                        ai_detail: float = 0.15,
                        ai_strength: float = 1.0) -> np.ndarray:
    """
    Extract height map from luminance of the diffuse texture.
    blur_radius  – Gaussian smooth (0 = off)
    contrast     – multiplier around 0.5 midpoint
    brightness   – additive offset (−1 … +1)
    invert       – flip height direction
    """
    # --- AI Depth path - gives you that reference quality ---
    if use_ai and _AI_OK and _ai is not None:
        try:
            from PIL import Image as _PILImage
            # Convert BGR numpy to PIL RGB
            if img.ndim == 3:
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                pil = _PILImage.fromarray(rgb)
            else:
                pil = _PILImage.fromarray(img).convert("RGB")
            # AI depth estimation - this is where we get reference-like smooth depth
            depth_pil = _ai.estimate_depth_pil(pil, model_type=ai_model, detail_blend=ai_detail, invert=False)
            h = np.array(depth_pil).astype(np.float32) / 255.0
            # Apply contrast/brightness on top of AI depth for artistic control
            h = np.clip((h - 0.5) * contrast * ai_strength + 0.5 + brightness, 0, 1)
            h = _blur(h, blur_radius) if blur_radius > 0 else h
            if invert:
                h = 1.0 - h
            return np.clip(h * 255, 0, 255).astype(np.uint8)
        except Exception as e:
            print(f"[Height] AI depth failed, falling back to luminance: {e}")

    # --- Fallback heuristic (original) ---
    h = _gray(img) / 255.0
    h = np.clip((h - 0.5) * contrast + 0.5 + brightness, 0, 1)
    h = _blur(h, blur_radius)
    if invert:
        h = 1.0 - h
    return np.clip(h * 255, 0, 255).astype(np.uint8)


def normal_from_height(height: np.ndarray,
                       scale: float = 3.0,
                       flip_x: bool = False,
                       flip_y: bool = True,
                       full_z: bool = True) -> np.ndarray:
    """Generate RGB normal map from height using Sobel gradients (Materialize method).
    full_z – when True, Z spans the whole 0-255 range (standard). When False,
             Z is compressed into the upper half (128-255) only, matching
             Materialize's "Full Z Range" toggle: engines that read Z as
             always-positive/steep get a stronger normal-strength look, at
             the cost of clipping perfectly-flat areas away from pure blue."""
    h = height.astype(np.float32) / 255.0
    dx = cv2.Sobel(h, cv2.CV_32F, 1, 0, ksize=3) * scale
    dy = cv2.Sobel(h, cv2.CV_32F, 0, 1, ksize=3) * scale
    if flip_x: dx = -dx
    if flip_y: dy = -dy
    dz = np.ones_like(h)
    length = np.sqrt(dx*dx + dy*dy + dz*dz)
    nx = np.clip((dx / length * 0.5 + 0.5) * 255, 0, 255)
    ny = np.clip((dy / length * 0.5 + 0.5) * 255, 0, 255)
    nz_unit = dz / length  # 0..1 (Z is never negative for a heightfield normal)
    if full_z:
        # Stretch the full 0..1 range across the whole 0-255 byte range.
        nz = np.clip(nz_unit * 255, 0, 255)
    else:
        # Compress into the upper half (128-255) -- matches most other
        # normal-map tools' default, since heightfield Z rarely dips low.
        nz = np.clip(128 + nz_unit * 127, 128, 255)
    # BGR stack — _save() converts BGR→RGB so final PNG: R=X, G=Y, B=Z
    return np.stack([nz, ny, nx], axis=-1).astype(np.uint8)


def normal_from_height_and_diffuse(height: np.ndarray,
                                    diffuse_img: Optional[np.ndarray] = None,
                                    diffuse_weight: float = 0.0,
                                    scale: float = 3.0,
                                    flip_x: bool = False,
                                    flip_y: bool = True,
                                    full_z: bool = True) -> np.ndarray:
    """
    Materialize's Normal-From-Height panel has a "Shape from Diffuse"
    checkbox that blends in gradient information taken directly from the
    diffuse image's luminance, not just the height map. This is a from-
    scratch approximation of that idea (their exact "Shape Recognition /
    Rotation / Spread / Bias" controls aren't visible from outside a
    compiled Unity build, so this isn't a byte-exact reproduction) --
    but the underlying concept is the same: blend the height-derived
    gradient with a diffuse-luminance-derived gradient before deriving
    the final normal.

    diffuse_weight -- 0.0 = pure height-based normal (identical output to
                      normal_from_height()), 1.0 = pure diffuse-luminance
                      shape, values between blend the two gradient fields.
    """
    h = height.astype(np.float32) / 255.0
    dx = cv2.Sobel(h, cv2.CV_32F, 1, 0, ksize=3)
    dy = cv2.Sobel(h, cv2.CV_32F, 0, 1, ksize=3)

    if diffuse_weight > 0 and diffuse_img is not None:
        gray = _gray(diffuse_img).astype(np.float32) / 255.0
        dx_d = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        dy_d = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        w = min(max(diffuse_weight, 0.0), 1.0)
        dx = dx * (1 - w) + dx_d * w
        dy = dy * (1 - w) + dy_d * w

    dx = dx * scale
    dy = dy * scale
    if flip_x: dx = -dx
    if flip_y: dy = -dy
    dz = np.ones_like(h)
    length = np.sqrt(dx*dx + dy*dy + dz*dz)
    nx = np.clip((dx / length * 0.5 + 0.5) * 255, 0, 255)
    ny = np.clip((dy / length * 0.5 + 0.5) * 255, 0, 255)
    nz_unit = dz / length
    if full_z:
        nz = np.clip(nz_unit * 255, 0, 255)
    else:
        nz = np.clip(128 + nz_unit * 127, 128, 255)
    return np.stack([nz, ny, nx], axis=-1).astype(np.uint8)


def ao_from_height(height: np.ndarray,
                   sample_radius: float = 4.0,
                   intensity: float = 1.5,
                   power: float = 2.0) -> np.ndarray:
    """
    Ambient occlusion from height map.
    Multi-scale blur comparison: areas lower than their surroundings are darker.
    """
    h = height.astype(np.float32) / 255.0
    ao = np.zeros_like(h)
    scales  = [sample_radius * 0.4, sample_radius, sample_radius * 2.0]
    weights = [1.0, 1.5, 0.8]
    total_w = sum(weights)
    for s, w in zip(scales, weights):
        if s <= 0:
            continue
        blurred = _blur(h, s)
        # High surrounding average vs low local = occluded
        ao += np.clip(blurred - h, 0, 1) * w
    ao = ao / total_w * intensity
    ao_out = np.power(np.clip(1.0 - ao, 0, 1), power)
    return np.clip(ao_out * 255, 0, 255).astype(np.uint8)


def ao_from_normal(normal_img: np.ndarray,
                    spread: float = 4.0,
                    intensity: float = 1.5,
                    power: float = 2.0) -> np.ndarray:
    """
    Ambient occlusion estimated from a normal map's curvature alone: pixels
    where neighboring normals converge (concave crevices) get darker. This
    is the 'Normal AO' side of Materialize's "Normal + Depth to AO" blend --
    it reacts to curvature already encoded in the normal map, so it still
    works even without a height map.
    normal_img -- BGR array as loaded by _load() (R=X, G=Y, B=Z per-pixel).
    """
    g = normal_img[:, :, 1].astype(np.float32) / 255.0  # Y
    r = normal_img[:, :, 2].astype(np.float32) / 255.0  # X
    nx = r * 2.0 - 1.0
    ny = g * 2.0 - 1.0

    dnx_dx = cv2.Sobel(nx, cv2.CV_32F, 1, 0, ksize=3)
    dny_dy = cv2.Sobel(ny, cv2.CV_32F, 0, 1, ksize=3)
    divergence = dnx_dx + dny_dy  # negative = concave / converging normals

    concavity = np.clip(-divergence, 0, None)
    if spread > 0:
        concavity = _blur(concavity, spread)

    ao = np.clip(1.0 - concavity * intensity, 0, 1)
    ao = np.power(ao, max(power, 0.01))
    return np.clip(ao * 255, 0, 255).astype(np.uint8)


def ao_from_normal_and_height(normal_img: Optional[np.ndarray],
                               height_img: Optional[np.ndarray],
                               spread: float = 4.0,
                               depth: float = 4.0,
                               blend: float = 1.0,
                               power: float = 1.0,
                               bias: float = 0.0) -> np.ndarray:
    """
    Materialize's "Normal + Depth to AO" panel: blend an AO pass computed
    from the normal map's curvature with one computed from the height map.
    blend  -- 0 = pure normal-based AO, 1 = pure height/depth-based AO
              (matches the panel's "Blend Normal AO and Depth AO" slider,
              default 1.0).
    spread -- panel's "AO pixel Spread" (blur radius for the normal-AO pass)
    depth  -- panel's "Pixel Depth" (sample radius for the height-AO pass)
    """
    normal_ao = None
    height_ao = None
    if normal_img is not None:
        normal_ao = ao_from_normal(normal_img, spread=spread, intensity=1.5, power=1.0).astype(np.float32)
    if height_img is not None:
        height_ao = ao_from_height(height_img, sample_radius=depth, intensity=1.5, power=1.0).astype(np.float32)

    if normal_ao is not None and height_ao is not None:
        ao = normal_ao * (1.0 - blend) + height_ao * blend
    elif height_ao is not None:
        ao = height_ao
    elif normal_ao is not None:
        ao = normal_ao
    else:
        raise ValueError('ao_from_normal_and_height needs a normal map, a height map, or both')

    ao = np.clip(ao / 255.0 + bias, 0, 1)
    ao = np.power(ao, max(power, 0.01))
    return np.clip(ao * 255, 0, 255).astype(np.uint8)


def roughness_from_diffuse(img: np.ndarray,
                            base_roughness: float = 0.65,
                            lum_influence: float = 0.4,
                            sat_influence: float = 0.25,
                            blur_radius: float = 1.5,
                            invert: bool = False) -> np.ndarray:
    """
    Roughness map from diffuse.
    Bright + saturated pixels → less rough (more specular).
    Dark + desaturated pixels → more rough.
    invert = True gives a Smoothness / Glossiness map.
    """
    lum = _gray(img) / 255.0
    if img.ndim >= 3:
        hsv = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2HSV)
        sat = hsv[:, :, 1].astype(np.float32) / 255.0
    else:
        sat = np.zeros_like(lum)

    rough = base_roughness + (0.5 - lum) * lum_influence - sat * sat_influence
    rough = np.clip(_blur(rough, blur_radius), 0, 1)
    if invert:
        rough = 1.0 - rough
    return np.clip(rough * 255, 0, 255).astype(np.uint8)


def metalness_from_diffuse(img: np.ndarray,
                            threshold: float = 0.55,
                            sharpness: float = 6.0,
                            blur_radius: float = 2.0) -> np.ndarray:
    """
    Metalness map.
    Metals tend to be bright (high Value) AND low saturation (grey tones).
    A sigmoid function provides a soft threshold.
    """
    if img.ndim >= 3:
        hsv = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2HSV)
        sat = hsv[:, :, 1].astype(np.float32) / 255.0
        val = hsv[:, :, 2].astype(np.float32) / 255.0
    else:
        sat = np.zeros(img.shape[:2], np.float32)
        val = img.astype(np.float32) / 255.0

    signal = val * (1.0 - sat)
    metal  = 1.0 / (1.0 + np.exp(-sharpness * (signal - threshold)))
    metal  = np.clip(_blur(metal, blur_radius), 0, 1)
    return np.clip(metal * 255, 0, 255).astype(np.uint8)


def edge_from_diffuse(img: np.ndarray,
                      blur_radius: float = 1.0,
                      threshold_low: int  = 40,
                      threshold_high: int = 130,
                      dilate: int  = 1,
                      soften: float = 0.5,
                      invert: bool = False) -> np.ndarray:
    """
    Edge / mask map using Canny edge detection.
    invert = True gives a bright-background edge mask.
    """
    gray = _gray(img).astype(np.uint8)
    gray = cv2.GaussianBlur(gray, (0, 0), blur_radius) if blur_radius > 0 else gray
    edges = cv2.Canny(gray, threshold_low, threshold_high)

    if dilate > 0:
        d = int(round(dilate))
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d * 2 + 1, d * 2 + 1))
        edges = cv2.dilate(edges, k, iterations=1)

    if soften > 0:
        ef = edges.astype(np.float32) / 255.0
        ef = cv2.GaussianBlur(ef, (0, 0), soften)
        edges = np.clip(ef * 255, 0, 255).astype(np.uint8)

    if invert:
        edges = 255 - edges
    return edges


def emissive_from_diffuse(img: np.ndarray,
                           threshold: float = 0.80,
                           falloff:   float = 0.12,
                           bloom_radius: float = 2.0) -> np.ndarray:
    """
    Emissive map — bright pixels above threshold glow.
    Returns a colour (BGR) map.
    """
    lum  = _gray(img) / 255.0
    mask = np.clip((lum - threshold) / max(falloff, 0.01), 0, 1)

    if img.ndim >= 3:
        rgb = img[:, :, :3].astype(np.float32) / 255.0
    else:
        rgb = np.stack([lum, lum, lum], axis=-1)

    emissive = rgb * mask[:, :, np.newaxis]
    if bloom_radius > 0:
        bloom = cv2.GaussianBlur(emissive.astype(np.float32), (0, 0), bloom_radius)
        emissive = np.maximum(emissive, bloom * 0.6)

    return np.clip(emissive * 255, 0, 255).astype(np.uint8)



# ── 8. RMAOS packer ───────────────────────────────────────────────────────────

# Available sources for each channel
RMAOS_CHANNEL_SOURCES = [
    'roughness',        # generated roughness map
    'smoothness',       # 1 - roughness
    'metalness',        # generated metalness map
    'ao',               # generated AO map
    'height',           # generated height map
    'edge',             # generated edge map
    'white',            # constant 255
    'black',            # constant 0
]

def rmaos_from_maps(maps: dict,
                    r_src: str = 'roughness',
                    g_src: str = 'metalness',
                    b_src: str = 'ao',
                    a_src: str = 'smoothness') -> np.ndarray:
    """
    Pack individual maps into a combined RMAOS texture.
    maps  – dict of {map_name: np.ndarray}  (all already generated)
    r_src – source map name for Red   channel  (Roughness)
    g_src – source map name for Green channel  (Metalness)
    b_src – source map name for Blue  channel  (AO)
    a_src – source map name for Alpha channel  (Specular / Smoothness)

    RMAOS convention used by Community Shaders:
      R = Roughness     G = Metalness     B = AO     A = Specular
    """
    def _resolve(src_name: str, ref_shape):
        h, w = ref_shape[:2]
        if src_name == 'white':
            return np.full((h, w), 255, np.uint8)
        if src_name == 'black':
            return np.zeros((h, w), np.uint8)

        # "smoothness" = 1 - roughness
        if src_name == 'smoothness':
            arr = maps.get('roughness')
            if arr is None:
                return np.full((h, w), 128, np.uint8)
            arr = _to_gray_u8(arr)
            return (255 - arr.astype(np.int16)).clip(0, 255).astype(np.uint8)

        arr = maps.get(src_name)
        if arr is None:
            return np.full((h, w), 128, np.uint8)
        arr = _to_gray_u8(arr)
        if arr.shape[:2] != (h, w):
            arr = cv2.resize(arr, (w, h), interpolation=cv2.INTER_LINEAR)
        return arr

    def _to_gray_u8(arr):
        if arr.ndim == 3:
            return cv2.cvtColor(arr[:, :, :3], cv2.COLOR_BGR2GRAY)
        return arr.astype(np.uint8)

    # Use roughness as reference size; fall back to any available map
    ref = maps.get('roughness')
    if ref is None:
        ref = next(iter(maps.values()), None)
    if ref is None:
        raise ValueError('No maps available to determine output dimensions.')

    h, w = ref.shape[:2]
    r_ch = _resolve(r_src, ref.shape)
    g_ch = _resolve(g_src, ref.shape)
    b_ch = _resolve(b_src, ref.shape)
    a_ch = _resolve(a_src, ref.shape)

    # OpenCV stores BGRA, PIL saves as RGBA — we pack for PIL output
    # so layout is: index 0=R, 1=G, 2=B, 3=A in numpy → PIL sees RGBA
    out = np.zeros((h, w, 4), np.uint8)
    out[:, :, 0] = r_ch   # R
    out[:, :, 1] = g_ch   # G
    out[:, :, 2] = b_ch   # B
    out[:, :, 3] = a_ch   # A
    return out

# ═════════════════════════════════════════════════════════════════════════════
# FULL PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

# Filename suffix per map
MAP_SUFFIXES = {
    'height':    '_p',
    'normal':    '_n',
    'ao':        '_ao',
    'roughness': '_r',
    'metalness': '_m',
    'edge':      '_edge',
    'emissive':  '_emissive',
    'rmaos':     '_rmaos',
}

MAP_ORDER = ['height', 'normal', 'ao', 'roughness', 'metalness', 'edge', 'emissive', 'rmaos']


def compute_map(map_name: str, img: np.ndarray, kw: dict,
                 height_map: Optional[np.ndarray] = None,
                 normal_map: Optional[np.ndarray] = None) -> np.ndarray:
    """
    Compute exactly one map from an already-loaded diffuse array (in memory,
    no disk I/O). Used by both generate_all_maps() (full-res, saves to disk)
    and the live-preview path (low-res, thumbnail only) so the two never
    drift out of sync with each other.

    `height_map`/`normal_map`, if given, are reused instead of recomputing
    (needed since a map's own sliders may differ from what its dependents
    were last generated with).
    """
    if map_name == 'height':
        return height_from_diffuse(img, **kw)
    elif map_name == 'normal':
        h = height_map if height_map is not None else height_from_diffuse(img)
        return normal_from_height_and_diffuse(h, img, **kw)
    elif map_name == 'ao':
        h = height_map if height_map is not None else height_from_diffuse(img)
        n = normal_map if normal_map is not None else normal_from_height(h)
        return ao_from_normal_and_height(n, h, **kw)
    elif map_name == 'roughness':
        return roughness_from_diffuse(img, **kw)
    elif map_name == 'metalness':
        return metalness_from_diffuse(img, **kw)
    elif map_name == 'edge':
        return edge_from_diffuse(img, **kw)
    elif map_name == 'emissive':
        return emissive_from_diffuse(img, **kw)
    raise ValueError(f'Unknown map: {map_name}')


def generate_all_maps(img_path: str,
                      settings: dict,
                      output_dir: str,
                      fmt: str = 'png',
                      log:       Callable = None,
                      progress:  Callable = None,
                      cancelled: Callable = None) -> Dict[str, str]:
    """
    Generate all enabled PBR maps from a single diffuse image.

    settings keys:
      enabled   – {map_name: bool}
      height    – kwargs for height_from_diffuse
      normal    – kwargs for normal_from_height
      ao        – kwargs for ao_from_normal_and_height (Normal + Depth blend)
      roughness – kwargs for roughness_from_diffuse
      metalness – kwargs for metalness_from_diffuse
      edge      – kwargs for edge_from_diffuse
      emissive  – kwargs for emissive_from_diffuse

    Returns {map_name: output_file_path} for each successfully generated map.
    """
    def _log(msg, c=None):  log      and log(msg, c)
    def _prog(d, t):        progress and progress(d, t)
    def _done():            return cancelled and cancelled()

    img = _load(img_path)
    if img is None:
        _log(f'Could not load: {img_path}', 'error')
        return {}

    diffuse_kw = settings.get('diffuse', {})
    if diffuse_kw:
        img = adjust_diffuse(img, **diffuse_kw)

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    stem    = Path(img_path).stem
    enabled = settings.get('enabled', {m: True for m in MAP_ORDER})
    total   = sum(1 for m in MAP_ORDER if enabled.get(m, True))

    _log(f'Source: {img_path}')
    _log(f'Output: {output_dir}')
    _log(f'Generating {total} map(s)...\n')
    _prog(0, total)

    results:    Dict[str, str] = {}
    height_map: Optional[np.ndarray] = None
    normal_map: Optional[np.ndarray] = None
    done = 0

    if enabled.get('normal', True) or enabled.get('ao', True):
        height_map = height_from_diffuse(img, **settings.get('height', {}))
    if enabled.get('ao', True):
        normal_map = normal_from_height_and_diffuse(height_map, img, **settings.get('normal', {}))

    for map_name in MAP_ORDER:
        if _done():
            _log('Cancelled.', 'warn')
            break
        if not enabled.get(map_name, True):
            continue

        _log(f'  [{map_name}]  generating...')
        try:
            kw = settings.get(map_name, {})
            result = compute_map(map_name, img, kw, height_map=height_map, normal_map=normal_map)
            if map_name == 'height':
                height_map = result
            elif map_name == 'normal':
                normal_map = result

            suffix   = MAP_SUFFIXES.get(map_name, f'_{map_name}')
            out_path = Path(output_dir) / f'{stem}{suffix}.{fmt.lower()}'
            _save(result, out_path)
            results[map_name] = str(out_path)
            _log(f'    ✓ {out_path.name}', 'success')

        except Exception as e:
            _log(f'    ✗ {map_name}: {e}', 'error')

        done += 1
        _prog(done, total)

    # ── Pack RMAOS from generated maps ───────────────────────────────────────
    if not _done() and enabled.get('rmaos', True):
        rmaos_kw   = settings.get('rmaos', {})
        rmaos_maps = {}
        for mn in ('roughness', 'metalness', 'ao', 'height', 'edge', 'emissive'):
            path = results.get(mn)
            if path and Path(path).exists():
                loaded = _load(path)
                if loaded is not None:
                    rmaos_maps[mn] = loaded
        if rmaos_maps:
            _log('  [rmaos]  packing channels...')
            try:
                rmaos_arr = rmaos_from_maps(
                    rmaos_maps,
                    r_src=rmaos_kw.get('r_src', 'roughness'),
                    g_src=rmaos_kw.get('g_src', 'metalness'),
                    b_src=rmaos_kw.get('b_src', 'ao'),
                    a_src=rmaos_kw.get('a_src', 'smoothness'))
                rmaos_path = Path(output_dir) / f'{stem}_rmaos.{fmt.lower()}'
                Image.fromarray(rmaos_arr).save(str(rmaos_path))
                results['rmaos'] = str(rmaos_path)
                _log(f'    ✓ {rmaos_path.name}', 'success')
            except Exception as e:
                _log(f'    ✗ rmaos: {e}', 'error')
        else:
            _log('  [rmaos]  skipped — no source maps available', 'warn')
        done += 1
        _prog(done, total)

    _log(f'\n✓ Complete — {len(results)}/{total} maps saved to {output_dir}',
         'success' if results else 'warn')
    return results
