"""
Pinta bridge - "round-trip external editor" integration, using
app/external_app.py's ExternalEditSession.

Pinta (https://github.com/PintaProject/Pinta, MIT) has no CLI or scripting
API to call into - it's a GTK/.NET GUI app. Unlike Upscayl, there's no
headless binary to shell out to here. The realistic integration is: launch
Pinta on a file, let the user actually edit in Pinta's own window, detect
when Pinta closes, and reload the file if it changed - exactly how
Photoshop/GIMP round-trip editing works in game engines and DCC tools.

This plugin also doubles as the template for any other GUI-only editor
you want to bridge later (GIMP, Photoshop, Krita, etc.) - only the exe
path and file-type assumptions would need to change.
"""
import os
from pathlib import Path

from PIL import Image
from imgui_bundle import imgui

from app.plugin_base import TG3Plugin
from app.external_app import ExternalEditSession
from app.gl_utils import pil_to_texture, texture_imgui_id
from app.file_dialogs import pick_file

_LEVEL_COLOR = {
    "error": imgui.ImVec4(0.96, 0.28, 0.28, 1.0),
    "success": imgui.ImVec4(0.31, 0.79, 0.69, 1.0),
    None: imgui.ImVec4(0.80, 0.80, 0.80, 1.0),
}


class PintaEditorPlugin(TG3Plugin):
    name = "Edit in Pinta"
    icon = "\u270e"
    order = 105

    def __init__(self, app):
        super().__init__(app)
        self.pinta_path = ""
        self.file_path = ""
        self.preview_img = None
        self.preview_tex = None
        self.status = ""
        self.session = ExternalEditSession()

    def _guess_pinta_path(self):
        """Best-effort default so most Windows users don't have to browse
        for it manually - only used to pre-fill the field once, never
        silently overrides a path the user already set."""
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Pinta\bin\Pinta.exe"),
            r"C:\Program Files\Pinta\bin\Pinta.exe",
            r"C:\Program Files (x86)\Pinta\bin\Pinta.exe",
            "/usr/bin/pinta",
            "/usr/local/bin/pinta",
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        return ""

    def on_activate(self):
        if not self.pinta_path:
            self.pinta_path = self._guess_pinta_path()

    def _load_preview(self, path):
        try:
            self.preview_img = Image.open(path).convert("RGB")
            self.preview_tex = pil_to_texture(self.app.gl, self.preview_img, self.preview_tex)
        except Exception as e:
            self.status = f"Preview load failed: {e}"

    def _open_in_pinta(self):
        if not self.pinta_path or not os.path.isfile(self.pinta_path):
            self.status = f"Pinta not found at: {self.pinta_path or '(not set)'}"
            return
        if not self.file_path or not os.path.isfile(self.file_path):
            self.status = "Pick a file to edit first."
            return
        self.session.start(self.pinta_path, self.file_path)
        self.status = f"Launched Pinta on {os.path.basename(self.file_path)} - waiting for it to close..."

    def gui(self):
        imgui.text_wrapped(
            "Pinta has no CLI or scripting API to call into, so this is a "
            "round-trip: launch Pinta on a file, edit it there, and this "
            "tab picks the result back up once Pinta closes and the file "
            "changed on disk.")
        imgui.separator()

        imgui.push_item_width(-90)
        _, self.pinta_path = imgui.input_text("##pinta_path", self.pinta_path, 1024)
        imgui.pop_item_width()
        imgui.same_line()
        if imgui.button("Browse##pinta_path"):
            f = pick_file(str(Path(self.pinta_path).parent) if self.pinta_path else None,
                           [("Pinta", "Pinta.exe;pinta"), ("All files", "*.*")])
            if f:
                self.pinta_path = f
        imgui.same_line()
        imgui.text("Pinta Path")

        imgui.push_item_width(-90)
        _, self.file_path = imgui.input_text("##pinta_file", self.file_path, 1024)
        imgui.pop_item_width()
        imgui.same_line()
        if imgui.button("Browse##pinta_file"):
            f = pick_file(self.app.input_dir,
                           [("Images", "*.png *.jpg *.jpeg *.bmp *.tga"), ("All files", "*.*")])
            if f:
                self.file_path = f
                self._load_preview(f)
        imgui.same_line()
        imgui.text("File to Edit")

        imgui.separator()
        busy = self.session.running
        if busy:
            imgui.begin_disabled()
        if imgui.button("Open in Pinta", imgui.ImVec2(-1, 32)):
            self._open_in_pinta()
        if busy:
            imgui.end_disabled()

        self.session.poll()
        if self.session.running:
            imgui.text_disabled(f"Pinta is open on {os.path.basename(self.session.file_path)} - editing...")
        elif self.session.done and self.session.file_path:
            if self.session.error:
                imgui.text_colored(_LEVEL_COLOR["error"], f"Could not launch Pinta: {self.session.error}")
            elif self.session.file_changed:
                imgui.text_colored(_LEVEL_COLOR["success"], "Pinta closed - file changed, reloaded below.")
                self._load_preview(self.session.file_path)
            else:
                imgui.text_disabled("Pinta closed - file was not modified.")

        if self.status:
            imgui.text(self.status)

        imgui.separator()
        if self.preview_tex is not None:
            avail = imgui.get_content_region_avail()
            size = max(min(avail.x, avail.y) - 10, 64)
            imgui.image(texture_imgui_id(self.preview_tex), imgui.ImVec2(size, size))

    def on_shutdown(self):
        if self.preview_tex is not None:
            self.preview_tex.release()
