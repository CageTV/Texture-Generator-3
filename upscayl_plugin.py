"""
Upscayl bridge - wraps the real upscayl-bin CLI (the headless binary
Upscayl itself is built on, forked from realesrgan-ncnn-vulkan; see
https://github.com/upscayl/upscayl-ncnn for the flag reference this plugin
follows) using app/proc_runner.py's StreamingProcess - the exact same
non-blocking pattern already proven out on Step1.ps1/Step2.ps1 in
pbr_json_builder_plugin.py.

Upscayl is AGPL-3.0. This plugin does not vendor any of its code - it
shells out to a copy of upscayl-bin you already have installed (Upscayl
ships it inside its own install folder), the same way this app already
shells out to texconv.exe/BSArch.exe/powershell.exe. Point the two path
fields below at your install once; TG3 doesn't bundle upscayl-bin itself.

CLI reference (confirmed against upscayl-ncnn's own docs):
  upscayl-bin -i <file_or_dir> -o <file_or_dir> -n <model> -s <scale>
              -m <models_folder> -f <png|jpg|webp> -c <0-100> -g <gpu_id>
              -t <tile_size> -x (TTA)  -v (verbose)
"""
import os
from pathlib import Path

from imgui_bundle import imgui

from app.plugin_base import TG3Plugin
from app.proc_runner import StreamingProcess
from app.file_dialogs import pick_file, pick_folder

_LEVEL_COLOR = {
    "error": imgui.ImVec4(0.96, 0.28, 0.28, 1.0),
    "warn": imgui.ImVec4(0.81, 0.57, 0.47, 1.0),
    "success": imgui.ImVec4(0.31, 0.79, 0.69, 1.0),
    None: imgui.ImVec4(0.80, 0.80, 0.80, 1.0),
}

# Common bundled model names as of upscayl-ncnn's default model set - a
# starting point, not exhaustive. Whatever's actually present in the
# models folder the user points at is what matters; this is just presets
# for the dropdown so most people don't have to type a model name by hand.
MODEL_PRESETS = [
    "realesrgan-x4plus",
    "realesrgan-x4plus-anime",
    "remacri",
    "ultramix-balanced",
    "ultrasharp",
    "digital-art-4x",
]

FORMATS = ["png", "jpg", "webp"]


def _draw_log(lines, child_id, height=200):
    imgui.begin_child(child_id, imgui.ImVec2(0, height), True)
    for text, level in lines:
        imgui.text_colored(_LEVEL_COLOR.get(level, _LEVEL_COLOR[None]), text)
    if lines:
        imgui.set_scroll_here_y(1.0)
    imgui.end_child()


class UpscaylPlugin(TG3Plugin):
    name = "Upscayl (AI Upscaler)"
    icon = "\u2b06"
    order = 100

    def __init__(self, app):
        super().__init__(app)
        self.bin_path = ""
        self.models_dir = ""
        self.batch_mode = False
        self.src = app.input_dir
        self.out = app.output_dir
        self.model = MODEL_PRESETS[0]
        self.scale = 4
        self.fmt = "png"
        self.compression = 0
        self.tta = False
        self.gpu_id = ""
        self.tile_size = ""
        self.proc = StreamingProcess()

    def _guess_upscayl_paths(self):
        """Best-effort defaults from Upscayl's actual default Windows
        install layout (C:\\Program Files\\Upscayl\\...) - only used to
        pre-fill the fields once, never overrides a path already set.
        Note: the CLI binary and the GUI look for models in two different
        default folders (a known Upscayl quirk) - resources\\bin\\models
        is the one upscayl-bin.exe itself actually reads from, so that's
        tried first."""
        bin_candidates = [
            r"C:\Program Files\Upscayl\resources\bin\upscayl-bin.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Upscayl\resources\bin\upscayl-bin.exe"),
            "/usr/lib/upscayl/resources/bin/upscayl-bin",
            "/opt/Upscayl/resources/bin/upscayl-bin",
        ]
        models_candidates = [
            r"C:\Program Files\Upscayl\resources\bin\models",
            r"C:\Program Files\Upscayl\resources\models",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Upscayl\resources\bin\models"),
        ]
        found_bin = next((c for c in bin_candidates if os.path.isfile(c)), "")
        found_models = next((c for c in models_candidates if os.path.isdir(c)), "")
        return found_bin, found_models

    def on_activate(self):
        if not self.bin_path or not self.models_dir:
            found_bin, found_models = self._guess_upscayl_paths()
            if not self.bin_path:
                self.bin_path = found_bin
            if not self.models_dir:
                self.models_dir = found_models

    def _build_cmd(self):
        cmd = [self.bin_path, "-i", self.src, "-o", self.out,
               "-n", self.model, "-s", str(int(self.scale)),
               "-f", self.fmt, "-c", str(int(self.compression)), "-v"]
        if self.models_dir:
            cmd += ["-m", self.models_dir]
        if self.tta:
            cmd.append("-x")
        if self.gpu_id.strip():
            cmd += ["-g", self.gpu_id.strip()]
        if self.tile_size.strip():
            cmd += ["-t", self.tile_size.strip()]
        return cmd

    def _run(self):
        if not self.bin_path or not os.path.isfile(self.bin_path):
            self.proc.lines.append((f"upscayl-bin not found at: {self.bin_path or '(not set)'}", "error"))
            self.proc.done = True
            return
        self.proc.start(self._build_cmd())

    def gui(self):
        imgui.text_wrapped(
            "Runs the real upscayl-bin CLI (Upscayl's own headless engine) "
            "as an external process - nothing here is bundled or vendored, "
            "since Upscayl is AGPL-3.0. Point this at your existing Upscayl "
            "install once.")
        imgui.separator()

        imgui.push_item_width(-90)
        _, self.bin_path = imgui.input_text("##up_bin", self.bin_path, 1024)
        imgui.pop_item_width()
        imgui.same_line()
        if imgui.button("Browse##up_bin"):
            f = pick_file(str(Path(self.bin_path).parent) if self.bin_path else None,
                           [("upscayl-bin", "upscayl-bin*"), ("All files", "*.*")])
            if f:
                self.bin_path = f
        imgui.same_line()
        imgui.text("upscayl-bin Path")

        imgui.push_item_width(-90)
        _, self.models_dir = imgui.input_text("##up_models", self.models_dir, 1024)
        imgui.pop_item_width()
        imgui.same_line()
        if imgui.button("Browse##up_models"):
            d = pick_folder(self.models_dir)
            if d:
                self.models_dir = d
        imgui.same_line()
        imgui.text("Models Folder (optional)")

        imgui.separator()
        _, self.batch_mode = imgui.checkbox("Batch folder mode (off = single file)", self.batch_mode)

        label_src = "Source Folder" if self.batch_mode else "Source Image"
        label_out = "Output Folder" if self.batch_mode else "Output Image"

        imgui.push_item_width(-90)
        _, self.src = imgui.input_text("##up_src", self.src, 1024)
        imgui.pop_item_width()
        imgui.same_line()
        if imgui.button("Browse##up_src"):
            picked = (pick_folder(self.src) if self.batch_mode else
                      pick_file(self.src, [("Images", "*.png *.jpg *.jpeg *.webp"), ("All files", "*.*")]))
            if picked:
                self.src = picked
        imgui.same_line()
        imgui.text(label_src)

        imgui.push_item_width(-90)
        _, self.out = imgui.input_text("##up_out", self.out, 1024)
        imgui.pop_item_width()
        imgui.same_line()
        if imgui.button("Browse##up_out"):
            picked = pick_folder(self.out) if self.batch_mode else pick_file(self.out)
            if picked:
                self.out = picked
        imgui.same_line()
        imgui.text(label_out)

        imgui.separator()
        for i, preset in enumerate(MODEL_PRESETS):
            clicked = imgui.radio_button(preset, self.model == preset)
            if clicked:
                self.model = preset
            if i < len(MODEL_PRESETS) - 1 and (i + 1) % 3 != 0:
                imgui.same_line()
        _, self.model = imgui.input_text("Model name", self.model, 128)
        imgui.text_disabled("Must match a model file actually present in the Models Folder above.")

        _, self.scale = imgui.slider_int("Scale", int(self.scale), 2, 8)
        for i, fmt in enumerate(FORMATS):
            clicked = imgui.radio_button(fmt.upper(), self.fmt == fmt)
            if clicked:
                self.fmt = fmt
            if i < len(FORMATS) - 1:
                imgui.same_line()
        _, self.compression = imgui.slider_int("Compression", int(self.compression), 0, 100)
        _, self.tta = imgui.checkbox("TTA mode (slower, sometimes cleaner)", self.tta)
        _, self.gpu_id = imgui.input_text("GPU ID (blank = auto)", self.gpu_id, 32)
        _, self.tile_size = imgui.input_text("Tile Size (blank = auto)", self.tile_size, 32)

        imgui.separator()
        ready = not self.proc.running
        if not ready:
            imgui.begin_disabled()
        if imgui.button("Run Upscayl", imgui.ImVec2(-1, 32)):
            self._run()
        if not ready:
            imgui.end_disabled()

        self.proc.poll()
        if self.proc.running:
            imgui.text_disabled("Running - upscayl-bin doesn't report a total, so no progress bar; watch the log.")
        elif self.proc.done:
            if self.proc.ok:
                imgui.text_colored(_LEVEL_COLOR["success"], "Upscayl finished.")
            else:
                imgui.text_colored(_LEVEL_COLOR["error"], f"Upscayl exited with code {self.proc.returncode}.")
        _draw_log(self.proc.lines, "##up_log")
