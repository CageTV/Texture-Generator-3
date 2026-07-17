"""
ui_widgets.py
Shared custom widgets for TG3's dark theme: a pill-shaped, subtly-3D button
(plain tk.Button can't do rounded corners) rendered with PIL. Drop-in enough
to replace tk.Button at most call sites: supports command callbacks, hover/
press feedback, and .configure(bg=..., fg=..., text=..., state=...).
"""

import tkinter as tk
import tkinter.font as tkfont
from PIL import Image, ImageDraw, ImageTk


# ── Color helpers ────────────────────────────────────────────────────────────
def _hex_to_rgb(h):
    h = h.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return '#%02x%02x%02x' % tuple(max(0, min(255, int(c))) for c in rgb)


def lighten(hex_color, amt):
    r, g, b = _hex_to_rgb(hex_color)
    return _rgb_to_hex((r + (255 - r) * amt, g + (255 - g) * amt, b + (255 - b) * amt))


def darken(hex_color, amt):
    r, g, b = _hex_to_rgb(hex_color)
    return _rgb_to_hex((r * (1 - amt), g * (1 - amt), b * (1 - amt)))


def widget_bg(widget, default='#252526'):
    """Best-effort read of a widget's own background, so a pill button's
    transparent rounded corners blend into whatever panel it sits on."""
    try:
        return widget.cget('bg')
    except Exception:
        try:
            return widget.cget('background')
        except Exception:
            return default


# ── Rounded pill button ──────────────────────────────────────────────────────
class RoundedButton(tk.Label):
    """A pill-shaped button with a subtle top-highlight/bottom-shadow 3D bevel,
    rendered via PIL (supersampled 4x then downsampled for anti-aliasing).

    Usage mirrors tk.Button closely enough to be a near drop-in:
        RoundedButton(parent, text='Go', command=fn, bg='#0078d4', fg='white')
    and later:
        btn.configure(state='disabled')
        btn.configure(text='New Label', bg='#4ec9b0')
    """

    _img_cache = {}  # (w,h,radius,color_hex) -> ImageTk.PhotoImage

    def __init__(self, parent, text='', command=None, bg='#0078d4', fg='white',
                 font=None, pad=(12, 6), radius=None, width=None, parent_bg=None, **kw):
        self._command = command
        self._bg = bg
        self._fg = fg
        self._font = font or ('Segoe UI', 9)
        self._pad = pad
        self._text = text
        self._disabled = False
        self._parent_bg = parent_bg or widget_bg(parent)

        f = tkfont.Font(font=self._font)
        text_w = max(f.measure(text), 1)
        text_h = f.metrics('linespace')
        pad_x, pad_y = pad
        self._w = width or (text_w + pad_x * 2)
        self._h = text_h + pad_y * 2
        self._radius = radius if radius is not None else self._h // 2

        self._img_normal = self._render(bg)
        self._img_hover = self._render(lighten(bg, 0.12))
        self._img_pressed = self._render(darken(bg, 0.15))
        self._img_disabled = self._render('#3a3a3a')

        kw.pop('image', None)
        super().__init__(parent, image=self._img_normal, text=text, compound='center',
                          font=self._font, fg=fg, bg=self._parent_bg,
                          bd=0, highlightthickness=0, cursor='hand2', **kw)

        self.bind('<Enter>', self._on_enter)
        self.bind('<Leave>', self._on_leave)
        self.bind('<ButtonPress-1>', self._on_press)
        self.bind('<ButtonRelease-1>', self._on_release)

    def _render(self, color_hex):
        key = (self._w, self._h, self._radius, color_hex)
        cached = RoundedButton._img_cache.get(key)
        if cached is not None:
            return cached
        scale = 4
        w, h, r = self._w * scale, self._h * scale, self._radius * scale
        base = _hex_to_rgb(color_hex)

        img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=base + (255,))

        # Convex top-highlight band for a "raised" 3D look
        hl = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(hl).rounded_rectangle([0, 0, w - 1, int(h * 0.55)], radius=r,
                                              fill=(255, 255, 255, 40))
        img = Image.alpha_composite(img, hl)

        # Thin darker base edge for depth
        sh = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([0, int(h * 0.86), w - 1, h - 1], radius=r,
                                              fill=(0, 0, 0, 40))
        img = Image.alpha_composite(img, sh)

        img = img.resize((self._w, self._h), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        RoundedButton._img_cache[key] = photo
        return photo

    def _on_enter(self, _e):
        if not self._disabled:
            super().configure(image=self._img_hover)

    def _on_leave(self, _e):
        if not self._disabled:
            super().configure(image=self._img_normal)

    def _on_press(self, _e):
        if not self._disabled:
            super().configure(image=self._img_pressed)

    def _on_release(self, e):
        if self._disabled:
            return
        super().configure(image=self._img_hover)
        if self._command and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self._command()

    def configure(self, **kw):
        need_rerender = 'bg' in kw or 'background' in kw
        if need_rerender:
            self._bg = kw.pop('bg', None) or kw.pop('background', None)
        if 'fg' in kw or 'foreground' in kw:
            fgv = kw.pop('fg', None) or kw.pop('foreground', None)
            self._fg = fgv
            kw['fg'] = fgv
        if 'text' in kw:
            self._text = kw['text']
        if 'command' in kw:
            self._command = kw.pop('command')
        if 'state' in kw:
            st = kw.pop('state')
            self._disabled = (st == 'disabled')
            kw['cursor'] = 'arrow' if self._disabled else 'hand2'
            kw['image'] = self._img_disabled if self._disabled else self._img_normal
        if need_rerender:
            self._img_normal = self._render(self._bg)
            self._img_hover = self._render(lighten(self._bg, 0.12))
            self._img_pressed = self._render(darken(self._bg, 0.15))
            kw.setdefault('image', self._img_disabled if self._disabled else self._img_normal)
        super().configure(**kw)

    config = configure
