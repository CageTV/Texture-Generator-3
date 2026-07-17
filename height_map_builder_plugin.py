"""
Height Map Generator - GL-shell port of texture_generator.py's 'Height Map
Generator' tab. Drives height_map_builder.process_folder() unchanged.

process_folder() only takes a `progress(done, total)` callback (no
log/cancelled), unlike pbr_engine.py's functions - the small adapter below
just shapes it to job_runner.ThreadedJob's universal
log/progress/cancelled contract so this plugin can reuse the same runner
as every other tab rather than writing a one-off thread here.
"""
from pathlib import Path

from imgui_bundle import imgui

from app.plugin_base import TG3Plugin
from app.job_runner import ThreadedJob
from app.file_dialogs import pick_folder

try:
    import height_map_builder as _hmb
    _HMB_OK = True
    _HMB_ERR = None
except Exception as e:
    _HMB_OK = False
    _HMB_ERR = f"{type(e).__name__}: {e}"

_LEVEL_COLOR = {
    "error": imgui.ImVec4(0.96, 0.28, 0.28, 1.0),
    "warn": imgui.ImVec4(0.81, 0.57, 0.47, 1.0),
    "success": imgui.ImVec4(0.31, 0.79, 0.69, 1.0),
    None: imgui.ImVec4(0.80, 0.80, 0.80, 1.0),
}

FORMATS = ["png", "tga", "bmp", "dds"]


def _run_process_folder(in_dir, out_dir, formats, strength, blur, texconv_path,
                         log=None, progress=None, cancelled=None):
    """Adapter: process_folder() has no log/cancelled hooks, so those are
    accepted here and simply unused - only progress is real."""
    if log:
        log(f"Scanning {in_dir} ...")
    count = _hmb.process_folder(in_dir, out_dir, formats=tuple(formats),
                                 strength=strength, blur=blur,
                                 texconv_path=texconv_path, progress=progress)
    if log:
        log(f"Processed {count} texture(s).", "success")
    return count


def _draw_log(lines, child_id, height=160):
    imgui.begin_child(child_id, imgui.ImVec2(0, height), True)
    for text, level in lines:
        imgui.text_colored(_LEVEL_COLOR.get(level, _LEVEL_COLOR[None]), text)
    if lines:
        imgui.set_scroll_here_y(1.0)
    imgui.end_child()


class HeightMapBuilderPlugin(TG3Plugin):
    name = "Height Map Generator"
    icon = "\u25b2"
    order = 50

    def __init__(self, app):
        super().__init__(app)
        self.src = app.input_dir
        self.out = app.output_dir
        self.strength = _hmb.STRENGTH if _HMB_OK else 6.0
        self.blur = _hmb.BLUR_RADIUS if _HMB_OK else 2.5
        self.selected_formats = {"png": True, "tga": False, "bmp": False, "dds": False}
        self.job = ThreadedJob()

    def _texconv_path(self):
        p = Path(self.app.project_root) / "texconv.exe"
        return str(p) if p.is_file() else None

    def gui(self):
        if not _HMB_OK:
            imgui.text_colored(_LEVEL_COLOR["error"], f"height_map_builder unavailable: {_HMB_ERR}")

        imgui.text_wrapped(
            "Batch-generates a height map (_p) and a normal map (_n) for "
            "every image in the source folder, GPU-accelerated when "
            "available. Files already ending in a known map suffix "
            "(_n, _g, _m, _p, _r, _ao, _h, _s, _e, _orm, _rmaos, _emissive) "
            "are skipped.")
        imgui.separator()

        imgui.push_item_width(-90)
        _, self.src = imgui.input_text("##hmb_src", self.src, 1024)
        imgui.pop_item_width()
        imgui.same_line()
        if imgui.button("Browse##hmb_src"):
            d = pick_folder(self.src)
            if d:
                self.src = d
        imgui.same_line()
        imgui.text("Source Folder")

        imgui.push_item_width(-90)
        _, self.out = imgui.input_text("##hmb_out", self.out, 1024)
        imgui.pop_item_width()
        imgui.same_line()
        if imgui.button("Browse##hmb_out"):
            d = pick_folder(self.out)
            if d:
                self.out = d
        imgui.same_line()
        imgui.text("Output Folder")

        imgui.separator()
        _, self.strength = imgui.slider_float("Normal Strength", self.strength, 0.5, 20.0)
        _, self.blur = imgui.slider_float("Height Blur Radius", self.blur, 0.0, 10.0)

        imgui.text("Output formats:")
        for fmt in FORMATS:
            _, self.selected_formats[fmt] = imgui.checkbox(fmt.upper(), self.selected_formats[fmt])
            if fmt != FORMATS[-1]:
                imgui.same_line()

        imgui.separator()
        formats = [f for f, on in self.selected_formats.items() if on] or ["png"]
        needs_texconv = "dds" in formats
        texconv_path = self._texconv_path()
        if needs_texconv and not texconv_path:
            imgui.text_colored(_LEVEL_COLOR["error"],
                                "DDS output selected but texconv.exe not found next to this app.")

        ready = _HMB_OK and not self.job.running and not (needs_texconv and not texconv_path)
        if not ready:
            imgui.begin_disabled()
        if imgui.button("Build Height + Normal Maps", imgui.ImVec2(-1, 32)):
            self.job.start(_run_process_folder, self.src, self.out, formats,
                            self.strength, self.blur, texconv_path)
        if not ready:
            imgui.end_disabled()

        self.job.poll()
        if self.job.progress_total:
            imgui.progress_bar(self.job.fraction, imgui.ImVec2(-1, 0))
        if self.job.done:
            if self.job.ok:
                imgui.text_colored(_LEVEL_COLOR["success"], f"Done - processed {self.job.result} texture(s).")
            else:
                imgui.text_colored(_LEVEL_COLOR["error"], f"Error: {self.job.error}")
        _draw_log(self.job.lines, "##hmb_log")
