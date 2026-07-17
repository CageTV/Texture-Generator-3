"""
PBR Generator - GL-shell port of texture_generator.py's 'PBR Generator' tab
group: PBR Builder, Parallax Generator, and both directions of Complex
Parallax <-> Community Shaders PBR conversion.

All four operations already live in pbr_engine.py as plain functions
matching the same log/progress/cancelled contract job_runner.ThreadedJob
drives - so this plugin is almost entirely UI, reusing ThreadedJob four
times over rather than inventing anything new (same pattern
pbr_json_builder_plugin.py's Python side already proved out).
"""
from imgui_bundle import imgui

from app.plugin_base import TG3Plugin
from app.job_runner import ThreadedJob
from app.file_dialogs import pick_folder

try:
    import pbr_engine as _pe
    _PBR_OK = True
    _PBR_ERR = None
except Exception as e:
    _PBR_OK = False
    _PBR_ERR = f"{type(e).__name__}: {e}"

_LEVEL_COLOR = {
    "error": imgui.ImVec4(0.96, 0.28, 0.28, 1.0),
    "warn": imgui.ImVec4(0.81, 0.57, 0.47, 1.0),
    "success": imgui.ImVec4(0.31, 0.79, 0.69, 1.0),
    None: imgui.ImVec4(0.80, 0.80, 0.80, 1.0),
}


def _draw_log(lines, child_id, height=160):
    imgui.begin_child(child_id, imgui.ImVec2(0, height), True)
    for text, level in lines:
        imgui.text_colored(_LEVEL_COLOR.get(level, _LEVEL_COLOR[None]), text)
    if lines:
        imgui.set_scroll_here_y(1.0)
    imgui.end_child()


def _draw_progress(fraction):
    imgui.progress_bar(fraction, imgui.ImVec2(-1, 0))


def _folder_field(label, value, dialog_key):
    imgui.push_item_width(-90)
    changed, value = imgui.input_text(f"##{label}{dialog_key}", value, 1024)
    imgui.pop_item_width()
    imgui.same_line()
    if imgui.button(f"Browse##{dialog_key}"):
        d = pick_folder(value)
        if d:
            value = d
    imgui.same_line()
    imgui.text(label)
    return value


class PBRGeneratorPlugin(TG3Plugin):
    name = "PBR Generator"
    icon = "\u26a0"
    order = 60

    def __init__(self, app):
        super().__init__(app)

        # PBR Builder
        self.build_src = app.input_dir
        self.build_out = app.output_dir
        self.build_flip_green = False
        self.build_job = ThreadedJob()

        # Parallax Generator
        self.par_src = app.input_dir
        self.par_out = app.output_dir
        self.par_mode = "both"  # 'complex' | 'pbr' | 'both'
        self.par_contrast = 0.4
        self.par_clamp_low = 60.0
        self.par_clamp_high = 200.0
        self.par_blur_radius = 4.0
        self.par_exclude = ""
        self.par_job = ThreadedJob()

        # Complex -> PBR
        self.c2p_src = app.input_dir
        self.c2p_out = app.output_dir
        self.c2p_job = ThreadedJob()

        # PBR -> Complex
        self.p2c_src = app.input_dir
        self.p2c_out = app.output_dir
        self.p2c_job = ThreadedJob()

    # ── shared texconv gate ──────────────────────────────────────────────
    def _texconv_ready(self):
        return _PBR_OK and _pe.texconv_available()

    def _texconv_warning(self):
        if not _PBR_OK:
            imgui.text_colored(_LEVEL_COLOR["error"], f"pbr_engine unavailable: {_PBR_ERR}")
        elif not _pe.texconv_available():
            imgui.text_colored(_LEVEL_COLOR["error"],
                                "texconv.exe not found next to this app - these tools need it.")

    # ── PBR Builder ──────────────────────────────────────────────────────
    def _pbr_builder_ui(self):
        imgui.text_wrapped(
            "Convert loose PBR maps (albedo, normal, roughness, metalness, "
            "AO, height) into packed Community Shaders DDS files.")
        imgui.separator()
        self.build_src = _folder_field("Source Folder", self.build_src, "build_src")
        self.build_out = _folder_field("Output Folder", self.build_out, "build_out")
        imgui.separator()
        _, self.build_flip_green = imgui.checkbox(
            "Flip Normal Green Channel (Y-flip for DirectX normals)", self.build_flip_green)
        imgui.text_disabled(
            "Matched by name keywords: albedo/diffuse/basecolor \u00b7 normal/normalgl\n"
            "roughness/rough \u00b7 metallic/metalness \u00b7 ao/occlusion \u00b7 height/displacement")
        imgui.separator()

        ready = self._texconv_ready() and not self.build_job.running
        if not ready:
            imgui.begin_disabled()
        if imgui.button("Build PBR Textures", imgui.ImVec2(-1, 32)):
            self.build_job.start(_pe.run_build_pbr, self.build_src, self.build_out,
                                  self.build_flip_green)
        if not ready:
            imgui.end_disabled()
        self._texconv_warning()

        self.build_job.poll()
        _draw_progress(self.build_job.fraction)
        if self.build_job.done:
            if self.build_job.ok:
                imgui.text_colored(_LEVEL_COLOR["success"], "PBR Builder complete.")
            else:
                imgui.text_colored(_LEVEL_COLOR["error"], f"Error: {self.build_job.error}")
        _draw_log(self.build_job.lines, "##build_log")

    # ── Parallax Generator ───────────────────────────────────────────────
    def _parallax_ui(self):
        imgui.text_wrapped(
            "Generate Complex Parallax _m and/or Community Shaders PBR "
            "textures from diffuse + normal. Height is derived via FFT if "
            "not present in the source.")
        imgui.separator()
        self.par_src = _folder_field("Source Folder", self.par_src, "par_src")
        self.par_out = _folder_field("Output Folder", self.par_out, "par_out")
        imgui.separator()

        imgui.text_colored(imgui.ImVec4(0.0, 0.47, 0.83, 1.0), "OUTPUT MODE")
        for val, label in [("complex", "Complex Parallax only"),
                            ("pbr", "Community Shaders PBR only"),
                            ("both", "Both (recommended)")]:
            clicked = imgui.radio_button(label, self.par_mode == val)
            if clicked:
                self.par_mode = val

        imgui.separator()
        imgui.text_colored(imgui.ImVec4(0.0, 0.47, 0.83, 1.0), "HEIGHT GENERATION")
        _, self.par_contrast = imgui.slider_float("Contrast", self.par_contrast, 0.0, 2.0)
        _, self.par_clamp_low = imgui.slider_float("Clamp Low", self.par_clamp_low, 0.0, 128.0)
        _, self.par_clamp_high = imgui.slider_float("Clamp High", self.par_clamp_high, 128.0, 255.0)
        _, self.par_blur_radius = imgui.slider_float("Blur Radius", self.par_blur_radius, 0.0, 12.0)
        _, self.par_exclude = imgui.input_text("Exclude keywords (comma-separated)", self.par_exclude, 256)

        imgui.separator()
        ready = self._texconv_ready() and not self.par_job.running
        if not ready:
            imgui.begin_disabled()
        if imgui.button("Generate Parallax Textures", imgui.ImVec2(-1, 32)):
            excl = [x.strip() for x in self.par_exclude.split(",") if x.strip()]
            cfg = {
                "default": {
                    "contrast_factor": self.par_contrast,
                    "clamp_low": self.par_clamp_low,
                    "clamp_high": self.par_clamp_high,
                    "blur_radius": self.par_blur_radius,
                },
                "exclude": excl,
            }
            self.par_job.start(_pe.run_generate_parallax, self.par_src, self.par_out,
                                self.par_mode, config_override=cfg)
        if not ready:
            imgui.end_disabled()
        self._texconv_warning()

        self.par_job.poll()
        _draw_progress(self.par_job.fraction)
        if self.par_job.done:
            if self.par_job.ok:
                imgui.text_colored(_LEVEL_COLOR["success"], "Parallax generation complete.")
            else:
                imgui.text_colored(_LEVEL_COLOR["error"], f"Error: {self.par_job.error}")
        _draw_log(self.par_job.lines, "##par_log")

    # ── Complex -> PBR ───────────────────────────────────────────────────
    def _c2p_ui(self):
        imgui.text_wrapped(
            "Convert Complex Parallax sets (_m) to Community Shaders PBR "
            "format. Needs: <n>.dds + <n>_n.dds + <n>_m.dds")
        imgui.separator()
        self.c2p_src = _folder_field("Source Folder", self.c2p_src, "c2p_src")
        self.c2p_out = _folder_field("Output Folder", self.c2p_out, "c2p_out")
        imgui.separator()
        imgui.text_disabled(
            "Output: <out>/textures/PBR/<original_path>/\n"
            "Generated: <n>_rmaos.dds \u00b7 <n>_p.dds")
        imgui.separator()

        ready = self._texconv_ready() and not self.c2p_job.running
        if not ready:
            imgui.begin_disabled()
        if imgui.button("Convert to PBR", imgui.ImVec2(-1, 32)):
            self.c2p_job.start(_pe.run_convert_to_pbr, self.c2p_src, self.c2p_out)
        if not ready:
            imgui.end_disabled()
        self._texconv_warning()

        self.c2p_job.poll()
        _draw_progress(self.c2p_job.fraction)
        if self.c2p_job.done:
            if self.c2p_job.ok:
                imgui.text_colored(_LEVEL_COLOR["success"], f"Complex -> PBR complete. ({self.c2p_job.result} set(s))")
            else:
                imgui.text_colored(_LEVEL_COLOR["error"], f"Error: {self.c2p_job.error}")
        _draw_log(self.c2p_job.lines, "##c2p_log")

    # ── PBR -> Complex ───────────────────────────────────────────────────
    def _p2c_ui(self):
        imgui.text_wrapped(
            "Convert Community Shaders PBR texture sets to Complex Parallax "
            "_m format. Needs: diffuse + _n normal + _p height (optional: "
            "_rmaos)")
        imgui.separator()
        self.p2c_src = _folder_field("Source Folder", self.p2c_src, "p2c_src")
        self.p2c_out = _folder_field("Output Folder", self.p2c_out, "p2c_out")
        imgui.separator()
        imgui.text_disabled(
            "_m channel packing:\n"
            "  R / B  -  metalness (from _rmaos if present)\n"
            "  G      -  brightness-adjusted from diffuse green\n"
            "  A      -  height (from _p)")
        imgui.separator()

        ready = self._texconv_ready() and not self.p2c_job.running
        if not ready:
            imgui.begin_disabled()
        if imgui.button("Convert to Complex Parallax", imgui.ImVec2(-1, 32)):
            self.p2c_job.start(_pe.run_convert_to_complex, self.p2c_src, self.p2c_out)
        if not ready:
            imgui.end_disabled()
        self._texconv_warning()

        self.p2c_job.poll()
        _draw_progress(self.p2c_job.fraction)
        if self.p2c_job.done:
            if self.p2c_job.ok:
                imgui.text_colored(_LEVEL_COLOR["success"], f"PBR -> Complex complete. ({self.p2c_job.result} set(s))")
            else:
                imgui.text_colored(_LEVEL_COLOR["error"], f"Error: {self.p2c_job.error}")
        _draw_log(self.p2c_job.lines, "##p2c_log")

    # ── ui ───────────────────────────────────────────────────────────────
    def gui(self):
        if imgui.begin_tab_bar("##pbr_gen_subtabs"):
            opened, _ = imgui.begin_tab_item("PBR Builder")
            if opened:
                self._pbr_builder_ui()
                imgui.end_tab_item()
            opened, _ = imgui.begin_tab_item("Parallax Generator")
            if opened:
                self._parallax_ui()
                imgui.end_tab_item()
            opened, _ = imgui.begin_tab_item("Complex -> PBR")
            if opened:
                self._c2p_ui()
                imgui.end_tab_item()
            opened, _ = imgui.begin_tab_item("PBR -> Complex")
            if opened:
                self._p2c_ui()
                imgui.end_tab_item()
            imgui.end_tab_bar()
