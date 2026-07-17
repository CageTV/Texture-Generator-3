"""
Applies TG3's existing dark palette (from texture_generator.py's `C` dict)
to Dear ImGui, so the new shell looks like a continuation of the old app
rather than a generic ImGui demo.
"""
from imgui_bundle import imgui

PALETTE = {
    'bg':          (0x1e, 0x1e, 0x1e),
    'surface':     (0x25, 0x25, 0x26),
    'panel':       (0x2d, 0x2d, 0x2d),
    'panel2':      (0x32, 0x32, 0x32),
    'input':       (0x3c, 0x3c, 0x3c),
    'border':      (0x47, 0x47, 0x47),
    'text':        (0xcc, 0xcc, 0xcc),
    'text_dim':    (0x85, 0x85, 0x85),
    'text_bright': (0xe8, 0xe8, 0xe8),
    'accent':      (0x00, 0x78, 0xd4),
    'accent_hi':   (0x1a, 0x86, 0xd9),
    'success':     (0x4e, 0xc9, 0xb0),
    'warn':        (0xce, 0x91, 0x78),
    'error':       (0xf4, 0x47, 0x47),
    'title':       (0x13, 0x13, 0x13),
}


def _c4(rgb, a=1.0):
    r, g, b = rgb
    return imgui.ImVec4(r / 255.0, g / 255.0, b / 255.0, a)


def apply_dark_theme():
    imgui.style_colors_dark()
    style = imgui.get_style()
    P = PALETTE

    style.window_rounding = 6
    style.frame_rounding = 4
    style.grab_rounding = 8
    style.tab_rounding = 4
    style.scrollbar_rounding = 6
    style.frame_padding = imgui.ImVec2(8, 5)
    style.item_spacing = imgui.ImVec2(8, 6)
    style.window_border_size = 1

    c = style.colors
    c[imgui.Col_.window_bg.value] = _c4(P['bg'])
    c[imgui.Col_.child_bg.value] = _c4(P['surface'])
    c[imgui.Col_.popup_bg.value] = _c4(P['panel'])
    c[imgui.Col_.border.value] = _c4(P['border'])
    c[imgui.Col_.frame_bg.value] = _c4(P['input'])
    c[imgui.Col_.frame_bg_hovered.value] = _c4(P['input'], 0.9)
    c[imgui.Col_.frame_bg_active.value] = _c4(P['accent'], 0.4)
    c[imgui.Col_.title_bg.value] = _c4(P['title'])
    c[imgui.Col_.title_bg_active.value] = _c4(P['title'])
    c[imgui.Col_.tab.value] = _c4(P['panel'])
    c[imgui.Col_.tab_hovered.value] = _c4(P['input'])
    c[imgui.Col_.tab_selected.value] = _c4(P['surface'])
    c[imgui.Col_.button.value] = _c4(P['accent'])
    c[imgui.Col_.button_hovered.value] = _c4(P['accent_hi'])
    c[imgui.Col_.button_active.value] = _c4(P['accent_hi'])
    c[imgui.Col_.header.value] = _c4(P['accent'], 0.5)
    c[imgui.Col_.header_hovered.value] = _c4(P['accent'], 0.7)
    c[imgui.Col_.check_mark.value] = _c4(P['success'])
    c[imgui.Col_.slider_grab.value] = _c4(P['accent'])
    c[imgui.Col_.slider_grab_active.value] = _c4(P['accent_hi'])
    c[imgui.Col_.text.value] = _c4(P['text'])
    c[imgui.Col_.text_disabled.value] = _c4(P['text_dim'])
    c[imgui.Col_.separator.value] = _c4(P['border'])
