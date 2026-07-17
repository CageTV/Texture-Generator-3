# TG3 GL Shell — Step 1

A new, separate entry point (`main_gl.py`) that runs alongside
`texture_generator.py`, not in place of it. Nothing in the existing app
changed. This is the foundation the rest of the plan (plugin migration,
Upscayl bridge, Blender bridge, mesh viewer, PyTorch-backed tools) builds on.

## Versioning

`version.py` (sits next to both `texture_generator.py` and `main_gl.py`)
holds the single `VERSION` string both apps display next to their title -
in the Tkinter header exactly where marked, and in the GL shell's top bar
and window title the same way. Current: **1.1.6**. Bump this one line on
every change from here on (1.1.6 → 1.1.7 → ...) - nothing else needs to
change to update the version shown in either app.

## What's actually different from the Tkinter version

- **One live GL context, not many headless ones.** `gpu_engine.py`,
  `gpu_preview.py`, and `ibl_preview.py` each call
  `moderngl.create_standalone_context()` because Tkinter can't host a real
  GL surface — every preview render had to happen off-screen, get read
  back into a PIL image, and get blitted into a Tk canvas. Here the window
  itself owns a GL context (`app_state.gl`), and plugins render straight
  into it or into an FBO inside it, then display the result with
  `imgui.image()` — no PIL round-trip.
- **Real-time orbit.** `material_preview.py`'s drag handler queued a
  re-render on mouse-up and showed a "Release to render" hint while
  dragging, because each render was a few-hundred-ms round trip through a
  worker thread. The new `MaterialPreviewPlugin` renders every frame the
  camera moves, so orbiting is a genuine live drag.
- **Plugins instead of hardcoded tabs.** `texture_generator.py`'s `_build_ui`
  wires all nine tabs directly into one 2800-line class. Here, each tab is
  an independent `.py` file in `plugins/` that the app discovers on
  startup — dropping a new file in is the entire integration step for
  anything new (a Blender bridge, an Upscayl bridge, whatever else).

## Running it

```
pip install -r requirements_gl_shell.txt
# plus texture_generator.py's own existing deps:
pip install pillow numpy moderngl glcontext opencv-python-headless
python main_gl.py
```

This has **not** been run in the sandbox that built it — that environment
has no GPU and no network access to install `imgui-bundle`/`moderngl`, so
only `python -m py_compile` (syntax) was checked, not an actual run. Please
run it on your machine and tell me what breaks; the most likely friction
point is version drift in `imgui-bundle`'s Python bindings (see the pinned
version note in `requirements_gl_shell.txt`) — specifically:
- `imgui.image(texture_id, size, uv0=..., uv1=...)`'s exact signature
- `imgui.Col_.<name>.value` enum access in `app/theme.py`
- `hello_imgui.RunnerParams()` field names in `main_gl.py`
- `imgui.begin_tab_item(label)` — assumed to return `(opened, p_open)`
  the same way `imgui.checkbox`/`imgui.slider_float` return
  `(changed, value)` tuples; used in `main_gl.py` and
  `pbr_generator_plugin.py`'s nested sub-tab bar. If your installed
  version returns a plain `bool` instead, drop the `, _` on those lines.

If any of those have moved in whatever version pip resolves, the fix is
almost always a one-line rename, not a structural change.

## The plugin contract (`app/plugin_base.py`)

```python
class TG3Plugin(ABC):
    name: str = "Unnamed Plugin"     # tab label
    icon: str = "\u2b21"             # tab glyph
    order: int = 100                 # left-to-right tab position

    def __init__(self, app):         # app = shared AppState
        self.app = app

    def on_activate(self):           # called once, first time tab is opened
        pass

    @abstractmethod
    def gui(self):                   # called every frame the tab is active
        raise NotImplementedError

    def on_shutdown(self):           # called once on app close
        pass
```

`app` (an `AppState` from `app/app_state.py`) gives every plugin:
- `app.input_dir` / `app.output_dir` — the shared folder fields
- `app.gl` — the live moderngl context (lazy; only touch it from inside
  `gui()`/`on_activate()`, never at import time or in `__init__`)
- `app.log(text, level)` — shared logging

Drop a new `.py` file defining a `TG3Plugin` subclass into `plugins/` and
it's picked up automatically — no edits to `main_gl.py` needed. A plugin
that fails to import or construct is logged and skipped, so one broken
add-on can't take the rest of the app down with it.

## What's included right now

Five working plugins, all proof-of-concept ports against your real engine
code (not stubs), plus two reusable runner helpers every remaining tab
can share:

- **`plugins/normal_map_plugin.py`** — ports "Normal Map Generator". Calls
  `gpu_engine.gpu_normal_from_image()` unchanged, with the same NumPy CPU
  fallback, and shows source + result as live GL textures.
- **`plugins/material_preview_plugin.py`** — a live GL viewport
  (Sphere/Cube/Cylinder/Plane). Reuses `gpu_preview.py`'s shader source and
  mesh generators *by import*, not copy-paste, so shading matches the
  existing Tkinter preview exactly.
- **`plugins/height_map_builder_plugin.py`** — ports "Height Map
  Generator". Calls `height_map_builder.process_folder()` unchanged via a
  tiny adapter (that function only has a `progress` callback, not the
  full `log`/`progress`/`cancelled` triple, so the adapter just shapes it
  to `ThreadedJob`'s contract).
- **`plugins/pbr_generator_plugin.py`** — ports the "PBR Generator" tab
  group as four sub-tabs (PBR Builder, Parallax Generator, Complex→PBR,
  PBR→Complex), each calling straight into `pbr_engine.py`
  (`run_build_pbr`, `run_generate_parallax`, `run_convert_to_pbr`,
  `run_convert_to_complex`) via `ThreadedJob`. Also gates each Generate
  button on `pbr_engine.texconv_available()`, matching the old
  `_need_texconv` check.
- **`plugins/pbr_json_builder_plugin.py`** — ports the split-pane "PBR
  JSON Builder": Python/keyword-driven generator on the left, PowerShell
  scaffold-then-fill workflow on the right. The proof point for both
  non-blocking patterns:
  - **`app/job_runner.py` (`ThreadedJob`)** — runs an in-process
    `pbr_engine.py`/`height_map_builder.py` function on a worker thread
    and lets `gui()` drain its queued log/progress once per frame. Every
    tab above already reuses this same class.
  - **`app/proc_runner.py` (`StreamingProcess`)** — runs an external
    process (`powershell.exe` running `Step1.ps1`/`Step2.ps1`) and
    streams its stdout the same non-blocking way, parsing the same
    `__SKYKING_TOTAL__`/`__SKYKING_PROGRESS__` markers the old
    `_run_ps_script` did. This is also the shape the **Upscayl bridge**
    and **Blender batch-mode bridge** will reuse.

None of `pbr_engine.py`, `height_map_builder.py`, `Step1.ps1`, nor
`Step2.ps1` changed at all.

## The two add-on bridges (Upscayl, Pinta)

These are the first "add-on" integrations from the original roadmap, and
they're deliberately built differently from each other, because the two
tools expose completely different surfaces:

- **`plugins/upscayl_plugin.py`** — Upscayl ships a real headless CLI
  binary, `upscayl-bin` (a fork of `realesrgan-ncnn-vulkan`, documented at
  https://github.com/upscayl/upscayl-ncnn). That's a clean fit for
  `StreamingProcess` — same non-blocking subprocess pattern as
  `Step1.ps1`/`Step2.ps1`. You point the plugin at your existing Upscayl
  install's `upscayl-bin` and models folder once; nothing is bundled or
  vendored, since Upscayl is **AGPL-3.0** — this app only ever shells out
  to a copy you already have, the same way it already shells out to
  `texconv.exe`/`BSArch.exe`.
- **`plugins/pinta_editor_plugin.py`** — Pinta has *no* CLI or scripting
  API at all; it's a GTK/.NET GUI app. So this is a different pattern
  entirely: **`app/external_app.py`'s `ExternalEditSession`** launches
  Pinta on a file, and just polls "has the process exited, did the file's
  mtime change" once per frame — a round-trip external editor, the same
  idea as Photoshop/GIMP round-tripping in game engines. Pinta is MIT, so
  there's no license reason to avoid it either way, but there's nothing
  to import — it's a full desktop app, not a library.

Use `ExternalEditSession` as the template for any other GUI-only tool you
want to bridge later (GIMP, Krita, Photoshop) — only the exe path and file
assumptions change. Use `StreamingProcess` for anything with a real
CLI/headless mode, the way the **Blender batch-mode bridge** from the
roadmap will.

## Porting the remaining 5 tabs

What's left from `texture_generator.py`: `_build_converters_tab` (Texture
Converters), `_build_bsa_tab` (BSA Utilities), `_build_dds_tool_tab` (DDS
Tool), `_build_dual_layer_tab` (Dual Layer Builder), and
`_build_material_tab` (Material Generator — full PBR set + RMAOS packer +
its own "Open Material Preview" button feeding `material_preview_plugin.py`).
Each becomes one new file in `plugins/`, following the same shape as the
five above:

1. Pull the tab's actual processing logic (it already mostly lives in
   `material_engine.py`, or direct `subprocess` calls to `texconv.exe`/
   `BSArch.exe`) — that code doesn't change at all.
2. Rebuild just the widget layer: `tk.Entry` → `imgui.input_text`,
   `ttk.Scale` → `imgui.slider_float`, `RoundedButton` → `imgui.button`,
   the `ScrolledText` console → `_draw_log()`-style colored text in a
   scrolling child window (copy the helper from the top of
   `pbr_generator_plugin.py` or `pbr_json_builder_plugin.py`).
3. For anything on a background thread: use `ThreadedJob` if it calls a
   `pbr_engine.py`/`material_engine.py` function with the
   `log`/`progress`/`cancelled` contract (or a thin adapter, as
   `height_map_builder_plugin.py` shows, if the function's callback shape
   is slightly different); use `StreamingProcess` if it shells out to
   `texconv.exe`/`BSArch.exe` and you want live streamed output — BSA
   Utilities is the main candidate for that.

**Material Generator** and **Dual Layer Builder** are the two structurally
biggest remaining ports, since they coordinate multiple engine calls plus
the live preview panel. Good candidates for the next session if you want
to keep going in order.

## Next steps (from the earlier roadmap)

1. ~~GL/ImGui shell~~ ← done
2. ~~Prove out both non-blocking patterns (in-process job + subprocess
   stream) against a real tab~~ ← done (PBR JSON Builder)
3. Port remaining tabs onto the plugin system:
   - ~~Normal Map Generator~~ ← done
   - ~~Height Map Generator~~ ← done
   - ~~PBR Generator (4 sub-tabs)~~ ← done
   - ~~PBR JSON Builder~~ ← done
   - Texture Converters, BSA Utilities, DDS Tool, Dual Layer Builder,
     Material Generator ← remaining (see above)
4. ~~Upscayl bridge~~ ← done (`plugins/upscayl_plugin.py`) — you'll need
   to point it at your own `upscayl-bin` and models folder the first time
   you use it; nothing is bundled automatically.
5. ~~Pinta bridge~~ ← done (`plugins/pinta_editor_plugin.py`) — same
   deal, point it at your Pinta install once (it tries a few common
   Windows/Linux paths automatically first).
6. Mesh viewer (`trimesh`/`assimp` import + GL rendering)
7. Blender bridge (batch mode via `blender --background --python`,
   a `StreamingProcess` consumer like Upscayl; GPL-3.0, same
   external-process reasoning)
8. PyTorch-backed custom tools, as concrete features come up
