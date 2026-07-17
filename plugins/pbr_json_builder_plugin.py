"""
PBR JSON Builder - GL-shell port of texture_generator.py's split-pane
'PBR JSON Builder' tab: Python/keyword-driven generator on the left,
PowerShell scaffold-then-fill workflow on the right, both writing the same
PBRNIFPatcher JSON schema.

This is the harder proof point after normal_map_plugin.py /
material_preview_plugin.py: it exercises both non-blocking patterns a
plugin will need going forward -
  - an in-process worker thread calling into pbr_engine.py directly
    (via app/job_runner.py's ThreadedJob), for the Python side
  - an external subprocess streaming stdout (via app/proc_runner.py's
    StreamingProcess), for the PowerShell side

Neither pbr_engine.py, Step1.ps1, nor Step2.ps1 changed at all to make
this work.
"""
import os
from pathlib import Path

from imgui_bundle import imgui

from app.plugin_base import TG3Plugin
from app.job_runner import ThreadedJob
from app.proc_runner import StreamingProcess
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


class PBRJsonBuilderPlugin(TG3Plugin):
    name = "PBR JSON Builder"
    icon = "\U0001f5d2"
    order = 90

    def __init__(self, app):
        super().__init__(app)
        self.project_root = Path(app.project_root)

        # Left (Python) state
        self.py_mod_dir = app.input_dir
        self.py_json_name = "my_mod_pbr"
        self.py_specular_level = 0.04
        self.py_roughness_scale = 1.0
        self.py_subsurface_opacity = 1.0
        self.py_smooth_angle = 75.0
        self.py_job = ThreadedJob()

        # Right (PowerShell) state
        self.ps_mod_dir = app.input_dir
        self.ps_specular_level = 0.04
        self.ps_roughness_scale = 1.0
        self.ps_subsurface_opacity = 1.0
        self.ps_displacement_scale = 1.0
        self.ps_multilayer_disp = 2.0
        self.ps_coat_strength = 1.0
        self.ps_coat_roughness = 1.0
        self.ps_coat_specular_level = 0.018
        self.ps_step1 = StreamingProcess()
        self.ps_step2 = StreamingProcess()
        self._run_both_pending_step2 = False

    # ── shared helpers ───────────────────────────────────────────────────
    def _powershell_exe(self):
        return str(self.project_root / "powershell.exe")

    def _script_path(self, name):
        return str(self.project_root / name)

    # ── left pane: Python ────────────────────────────────────────────────
    def _run_python_generate(self):
        if not _PBR_OK:
            self.py_job.lines.append((f"pbr_engine unavailable: {_PBR_ERR}", "error"))
            return
        cfg = {"defaults": {
            "specular_level": self.py_specular_level,
            "roughness_scale": self.py_roughness_scale,
            "subsurface_opacity": self.py_subsurface_opacity,
            "smooth_angle": self.py_smooth_angle,
        }}
        self.py_job.start(_pe.run_generate_json, self.py_mod_dir, self.py_json_name,
                           config_override=cfg)

    def _left_pane(self):
        imgui.text_wrapped(
            "Scans a mod folder for texture sets (needs at least diffuse + "
            "normal - height/rmaos/glow/fuzz/subsurface/coat are all optional "
            "and auto-detected) and writes a PBRNIFPatcher JSON. Extra config "
            "lives in config.json (keyword + per-file overrides).")
        imgui.separator()

        imgui.push_item_width(-90)
        _, self.py_mod_dir = imgui.input_text("##py_mod", self.py_mod_dir, 1024)
        imgui.pop_item_width()
        imgui.same_line()
        if imgui.button("Browse##py_mod"):
            d = pick_folder(self.py_mod_dir)
            if d:
                self.py_mod_dir = d
        imgui.same_line()
        imgui.text("Mod Folder")

        _, self.py_json_name = imgui.input_text("JSON Name", self.py_json_name, 256)

        imgui.separator()
        imgui.text_disabled(
            "emissive / parallax / subsurface / multilayer / fuzz are "
            "auto-detected per texture - these only set static fallbacks.")
        _, self.py_specular_level = imgui.slider_float("Specular Level", self.py_specular_level, 0.0, 1.0)
        _, self.py_roughness_scale = imgui.slider_float("Roughness Scale", self.py_roughness_scale, 0.0, 2.0)
        _, self.py_subsurface_opacity = imgui.slider_float("Subsurface Opacity", self.py_subsurface_opacity, 0.0, 1.0)
        _, self.py_smooth_angle = imgui.slider_float("Smooth Angle", self.py_smooth_angle, 0.0, 180.0)

        imgui.separator()
        disabled = self.py_job.running
        if disabled:
            imgui.begin_disabled()
        if imgui.button("Generate PBR JSON", imgui.ImVec2(-1, 32)):
            self._run_python_generate()
        if disabled:
            imgui.end_disabled()

        self.py_job.poll()
        _draw_progress(self.py_job.fraction)
        if self.py_job.done:
            if self.py_job.ok:
                count, path = self.py_job.result
                imgui.text_colored(_LEVEL_COLOR["success"], f"Done - wrote {count} entries to {path}")
            else:
                imgui.text_colored(_LEVEL_COLOR["error"], f"Error: {self.py_job.error}")
        _draw_log(self.py_job.lines, "##py_log")

    # ── right pane: PowerShell ───────────────────────────────────────────
    def _step2_args(self):
        return [
            "-ModRoot", self.ps_mod_dir,
            "-SpecularLevel", str(self.ps_specular_level),
            "-RoughnessScale", str(self.ps_roughness_scale),
            "-SubsurfaceOpacity", str(self.ps_subsurface_opacity),
            "-DisplacementScale", str(self.ps_displacement_scale),
            "-MultilayerDisplacementScale", str(self.ps_multilayer_disp),
            "-CoatStrength", str(self.ps_coat_strength),
            "-CoatRoughness", str(self.ps_coat_roughness),
            "-CoatSpecularLevel", str(self.ps_coat_specular_level),
        ]

    def _run_step(self, streaming_process, script_name, args):
        ps = self._powershell_exe()
        script = self._script_path(script_name)
        if not os.path.isfile(ps):
            streaming_process.lines.append((f"powershell.exe not found at {ps}", "error"))
            streaming_process.done = True
            return
        if not os.path.isfile(script):
            streaming_process.lines.append((f"{script_name} not found at {script}", "error"))
            streaming_process.done = True
            return
        cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script] + args
        streaming_process.start(cmd)

    def _run_step1(self, chain_step2=False):
        self._run_both_pending_step2 = chain_step2
        self._run_step(self.ps_step1, "Step1.ps1", ["-ModRoot", self.ps_mod_dir])

    def _run_step2(self):
        self._run_step(self.ps_step2, "Step2.ps1", self._step2_args())

    def _right_pane(self):
        imgui.text_wrapped(
            "Two-step workflow: Step 1 scaffolds a template JSON per diffuse "
            "texture found under Textures\\PBR. Step 2 fills each one in from "
            "whichever _d/_g/_f/_p/_s/_cnr maps sit next to it (and renames "
            'the diffuse file if the JSON is named "..._d").')
        imgui.separator()

        imgui.push_item_width(-90)
        _, self.ps_mod_dir = imgui.input_text("##ps_mod", self.ps_mod_dir, 1024)
        imgui.pop_item_width()
        imgui.same_line()
        if imgui.button("Browse##ps_mod"):
            d = pick_folder(self.ps_mod_dir)
            if d:
                self.ps_mod_dir = d
        imgui.same_line()
        imgui.text("Mod Folder")

        imgui.separator()
        _, self.ps_specular_level = imgui.slider_float("Specular Level##ps", self.ps_specular_level, 0.0, 1.0)
        _, self.ps_roughness_scale = imgui.slider_float("Roughness Scale##ps", self.ps_roughness_scale, 0.0, 2.0)
        _, self.ps_subsurface_opacity = imgui.slider_float("Subsurface Opacity##ps", self.ps_subsurface_opacity, 0.0, 1.0)
        _, self.ps_displacement_scale = imgui.slider_float("Displacement Scale", self.ps_displacement_scale, 0.0, 2.0)
        _, self.ps_multilayer_disp = imgui.slider_float("Multilayer Displacement", self.ps_multilayer_disp, 0.0, 3.0)
        _, self.ps_coat_strength = imgui.slider_float("Coat Strength", self.ps_coat_strength, 0.0, 1.0)
        _, self.ps_coat_roughness = imgui.slider_float("Coat Roughness", self.ps_coat_roughness, 0.0, 1.0)
        _, self.ps_coat_specular_level = imgui.slider_float("Coat Specular Level", self.ps_coat_specular_level, 0.0, 0.2)

        imgui.separator()
        busy = self.ps_step1.running or self.ps_step2.running
        if busy:
            imgui.begin_disabled()
        if imgui.button("Run Both Steps", imgui.ImVec2(-1, 32)):
            self._run_step1(chain_step2=True)
        if imgui.button("Step 1 only", imgui.ImVec2((imgui.get_content_region_avail().x - 8) / 2, 0)):
            self._run_step1(chain_step2=False)
        imgui.same_line()
        if imgui.button("Step 2 only", imgui.ImVec2(-1, 0)):
            self._run_step2()
        if busy:
            imgui.end_disabled()

        # Poll both streams and chain step 2 after step 1 finishes successfully.
        self.ps_step1.poll()
        self.ps_step2.poll()
        if (self._run_both_pending_step2 and self.ps_step1.done
                and not self.ps_step2.running and not self.ps_step2.done):
            self._run_both_pending_step2 = False
            if self.ps_step1.ok:
                self._run_step2()

        imgui.text_disabled("Step 1")
        _draw_progress(self.ps_step1.fraction)
        _draw_log(self.ps_step1.lines, "##ps1_log", height=110)

        imgui.text_disabled("Step 2")
        _draw_progress(self.ps_step2.fraction)
        _draw_log(self.ps_step2.lines, "##ps2_log", height=110)

    # ── ui ───────────────────────────────────────────────────────────────
    def gui(self):
        if not _PBR_OK:
            imgui.text_colored(_LEVEL_COLOR["error"], f"pbr_engine unavailable: {_PBR_ERR}")

        avail = imgui.get_content_region_avail()
        col_w = (avail.x - 16) / 2

        imgui.begin_child("##json_left", imgui.ImVec2(col_w, 0), True)
        imgui.text_colored(imgui.ImVec4(0.0, 0.47, 0.83, 1.0), "PYTHON  \u00b7  KEYWORD-DRIVEN")
        self._left_pane()
        imgui.end_child()

        imgui.same_line()

        imgui.begin_child("##json_right", imgui.ImVec2(col_w, 0), True)
        imgui.text_colored(imgui.ImVec4(0.0, 0.47, 0.83, 1.0), "POWERSHELL  \u00b7  SCAFFOLD + FILL")
        self._right_pane()
        imgui.end_child()
