"""
Small helpers for moving images between PIL and the GPU. Plugins use these
instead of each reimplementing texture upload, so a PIL image a plugin
already produced (e.g. a generated normal map) can go straight on screen.
"""
from PIL import Image


def pil_to_texture(ctx, img: Image.Image, existing=None):
    """Upload a PIL image to a moderngl texture, flipped for OpenGL's
    bottom-up convention. Reuses `existing` in-place if it's already the
    right size (cheap .write()), otherwise (re)allocates."""
    img = img.convert("RGBA").transpose(Image.FLIP_TOP_BOTTOM)
    w, h = img.size
    data = img.tobytes()

    if existing is not None and existing.size == (w, h):
        existing.write(data)
        return existing
    if existing is not None:
        existing.release()

    tex = ctx.texture((w, h), 4, data)
    tex.filter = (ctx.LINEAR, ctx.LINEAR)
    tex.build_mipmaps()
    return tex


def texture_imgui_id(tex):
    """moderngl.Texture -> the raw GL texture name imgui.image() expects."""
    return tex.glo
