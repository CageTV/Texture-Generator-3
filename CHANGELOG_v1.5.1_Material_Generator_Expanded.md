# TEXTURE GENERATOR - CHANGELOG
## v1.1.7 (Original) -> v1.1.13 (Current) -> v1.5.1 (This Build)

---

### 1. MATERIAL GENERATOR - Major Expansion
#### Original (v1.1.7) - WIP_TG2_9.png
Left panel had minimal controls:
- HEIGHT: Blur, Contrast, Brightness (3 sliders)
- NORMAL: Scale only (1 slider)
- AO: Radius, Intensity, Power (3 sliders)
- ROUGHNESS: Base, Lum Infl. (2 sliders)
- METALNESS: Threshold, Sharpness (2 sliders)
- EDGE: Blur, Low Thr, High Thr (3 sliders)
- EMISSIVE: Threshold, Falloff, Bloom (3 sliders)
- Grid: 8 maps + RMAOS packed at bottom
- No invert/flip toggles, no advanced AO

#### New (v1.1.13) - image_4266b7.png
Complete overhaul of all map generators:

**HEIGHT:**
- Added: Invert checkbox
- Now: Blur, Contrast, Brightness, Invert
- Better heightmap control for parallax

**NORMAL:**
- Added: Diffuse Weight (0.00), Full Z, Flip Y, Flip X checkboxes
- Before: Scale only (8.00)
- Now: Scale + Diffuse Weight + Full Z + Flip Y + Flip X
- Allows DirectX vs OpenGL normal flipping and detail control

**AO (Ambient Occlusion):**
- Complete rewrite from simple Radius/Intensity/Power
- Now: Pixel Spread (4.00), Pixel Depth (4.00), Blend N/D (1.00), Power (1.00), Bias (0.00)
- Much more accurate AO with spread/depth control and N/D blending

**ROUGHNESS:**
- Added: Sat Infl. (Saturation Influence), Blur, Smooth toggle
- Before: Base, Lum Infl. only
- Now: Base (0.65), Lum Infl. (0.48), Sat Infl. (0.25), Blur (1.50), Smooth checkbox
- Better roughness from luminance + saturation

**METALNESS:**
- Added: Blur slider
- Now: Threshold (0.55), Sharpness (6.00), Blur (2.00)
- Softens metalness detection for less noisy results

**EDGE (Curvature/Edge Wear):**
- Added: Dilate, Soften, Invert
- Before: Blur, Low Thr, High Thr only
- Now: Blur (1.00), Low Thr (40.00), High Thr (130.00), Dilate (1.00), Soften (0.50), Invert checkbox
- Full edge wear pipeline with dilation and softening

**EMISSIVE:**
- Kept: Threshold (0.80), Falloff (0.12), Bloom (2.00)
- Improved algorithm behind same controls

**RMAOS CHANNEL PACKER:**
- New: Auto-packed after generation
- R = Roughness, G = Metalness, B = AO, A = Specular
- Visible at bottom: RMAOS (packed) preview
- Checkbox: RMAOS auto-packed after generation

**UI:**
- Left controls now scrollable with much more parameters visible
- All sections have X toggle to enable/disable map
- Right grid still 4x2 + RMAOS packed, but larger previews

---

### 2. DUAL LAYER BUILDER - Complete Rework

#### v1.1.12 - Original
- 6 sliders only
- Metallic = flat value
- Roughness = inverted luminance only
- Height = luminance only
- Coat = flat color
- No real-time, no scroll, tiny 170px previews

#### v1.2 - Full Controls Upgrade
- BASE COLOR: Brightness (-0.5 to 0.5), Contrast (0.5-2.0), Saturation (0-2.0)
- NORMAL: Blur 0-10, Z Strength 0.1-5.0, Detail 0-1, Flip X/Y, Mode Sobel/Scharr
- ROUGHNESS: Contrast, Min/Max clamp 0-1, Gamma 0.1-3.0, Blur 0-10, AO Mix 0-1, Invert toggle
- METALLIC UPGRADED: Modes Flat/Threshold/Color Detect, Thresh 0-1, Toler 0-0.5, Contrast 0.5-3.0, Blur
- HEIGHT/PARALLAX: Source Luminance/Red Channel/Average, Contrast, Brightness, Blur, Midlevel 0-1, Parallax Scale 0-0.1, Invert
- CLEAR COAT: Opacity, Roughness, Normal Strength 0-2, Metallic 0-1, Parallax 0-0.1, Coat Color picker, Use blurred normal toggle
- AO/CURVATURE: Strength 0-2, Radius 0-10 generated from height
- Performance: cached luminance array, single cv2 blur passes

#### v1.3 - Live Preview + Scroll Fix
- Added Live Preview checkbox (on by default) at top
- All sliders use trace_add('write') -> debounced 150ms _dl_schedule_live() -> _dl_generate
- Checkboxes and comboboxes also trigger live
- Left panel width 380->400, added 80px bottom spacer so AO/Save buttons reachable
- Canvas stored as self._dl_scroll_canvas for proper wheel handling
- Fixed IndentationError at _build_dual_layer_tab line 3060 (methods at column 0)

#### v1.4 - Layout Flip - Maps on Top, Controls Bottom
- Flipped layout based on screenshot feedback: huge empty black space with 170px thumbs
- Now: TOP = 8 big previews in 4x2 grid, responsive 200-450px (260px start)
- In 1920x1080 fullscreen, thumbs ~360-400px, fills empty space
- BOTTOM = controls in 4 columns (400px tall, scrollable):
  Col 0: Base Color + Normal Map
  Col 1: Roughness + Metallic Upgraded
  Col 2: Height/Parallax + AO/Curvature
  Col 3: Clear Coat + Generate/Save/Preview
- Responsive thumb calc on <Configure>: avail // 4 - 24, re-renders maps at new size
- Top preview canvas has its own scrollbar, bottom controls has its own

---

### 3. PBR JSON BUILDER - Responsive Fix

#### v1.1.13 - Before (Screenshot)
- _build_json_tab used left_half/right_half pack side=left fill=both expand=True
- _pbr_layout left panel fixed width=440 + pack_propagate(False)
- Two tools side by side, no weights, no resize handling
- When not fullscreen, left tool log gets cut off

#### v1.5 - Now
- _build_json_tab now uses PanedWindow orient=horizontal, sashwidth=6, showhandle=True
- left_half minsize=380 width=700 stretch=always, right_half same - draggable divider
- User can drag divider to give more space to left or right
- When window resizes, both halves stretch proportionally
- _pbr_layout rewritten as responsive grid:
  - container column 0 weight=1 minsize=360 (controls), column 1 weight=2 (log gets 2x space)
  - left_wrapper scrollable via _make_scrollable (was fixed)
  - right log frame now uses ONLY pack (was mixing pack+grid causing TclError: cannot use geometry manager grid inside which already has slaves managed by pack)
  - Fixed crash: File texture_generator.py line 2154 _pbr_layout _tkinter.TclError
- Result: No cut-off in windowed mode, logs expand, controls scrollable

---

### 4. Bug Fixes
- Fixed IndentationError at line 3060 _build_dual_layer_tab (def at column 0)
- Fixed IndentationError at _dl_update_cell (def at column 0)
- Fixed TclError pack/grid mix in _pbr_layout
- Fixed scrollregion not expanding for new Dual Layer controls
- Fixed live preview requiring Generate All 8 Maps button

---

### 5. Performance
- Dual Layer: luminance cached, single cv2 blur passes
- Live preview debounced 150ms to avoid spamming generate on 4K textures
- Toggle Live Preview off for huge images

---

### Build Info
- Version: v1.1.13 base + v1.5.1 patches
- Files: texture_generator.py (182k+)
- Dependencies: PIL, numpy, opencv-python, ui_widgets, RoundedButton
- Build: PyInstaller dist\TG3.exe + texconv.exe + BSArch.exe

---

### Next Suggested
- Add RMAOS channel packer to Dual Layer Builder (like Material Generator)
- Add preset save/load for Dual Layer settings
- Add batch mode for Dual Layer
